"""Session Profile — OHLC, VAP, POC, Value Area, Initial Balance, POC drift.

Sessions are UTC-based windows with explicit Pre-Session phases:

  Asia         00:00-08:00
  Pre-London   06:00-08:00  (overlap with Asia tail)
  London       08:00-13:00
  Pre-NY       12:00-13:00  (overlap with London tail)
  New York     13:00-21:00
  Pre-Asia     21:00-00:00  (overlap with NY tail)

Phase transitions archive the current profile and start a fresh one.

Die gemeinsame Profil-Maschinerie (VAP/POC/VA/Regime/Archiv) lebt seit
Sprint B in core/volume_profile.py — hier nur Session-Spezifika.

Alle Schwellwerte kommen aus ProfileConfig (core/profile_config.py).
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.profile_config import ProfileConfig, PROFILE_CONFIG, resolve_config

# Re-Export für bestehende Importe (weekly_profile, composite_profile, tests)
from core.volume_profile import (  # noqa: F401
    BUCKET, BaseVolumeProfile, ProfileSnapshot,
    _bucket, _compute_ohlc, _compute_poc, _compute_value_area,
)

SESSION_DEFS: List[Dict[str, Any]] = [
    {"name": "Pre-Asia",      "start": 21,  "end": 24},
    {"name": "Asia",           "start": 0,   "end": 8},
    {"name": "Pre-London",     "start": 6,   "end": 8},
    {"name": "London",         "start": 8,   "end": 13},
    {"name": "Pre-NY",         "start": 12,  "end": 13},
    {"name": "New York",       "start": 13,  "end": 21},
]

SESSION_BY_HOUR: Dict[int, Dict[str, Any]] = {}
for sd in SESSION_DEFS:
    for h in range(sd["start"], sd["end"]):
        SESSION_BY_HOUR[h] = sd


def session_name_for_hour(hour_utc: int) -> str:
    sd = SESSION_BY_HOUR.get(hour_utc)
    return sd["name"] if sd else "Off-Hours"


def is_pre_session(session_name: str) -> bool:
    return session_name.startswith("Pre-")


# ---------------------------------------------------------------------------
# Benannte Session-Phasen (SYSTEM_ARCHITECTURE_V2.md §4.3)
#
# Feiner als SESSION_DEFS (Minuten-Auflösung wegen NY Open 13:30 UTC) und
# lückenlos über 24h — "Benannte Zustände, keine impliziten Lücken".
# Die Spec-Fenster ny_open (13:30–15:30) und overlap (13:30–16:00)
# überschneiden sich; hier deterministisch aufgelöst: ny_open gewinnt,
# overlap deckt den restlichen London/NY-Überlapp 15:30–16:00 ab.
# ---------------------------------------------------------------------------

PHASE_DEFS: List[Tuple[str, int, int]] = [
    # (phase, start minute-of-day UTC, end minute-of-day UTC)
    ("asia",            0,        7 * 60),
    ("london_pre",      7 * 60,   8 * 60),
    ("london_open",     8 * 60,   10 * 60),
    ("london_session", 10 * 60,   12 * 60),
    ("ny_pre",         12 * 60,   13 * 60 + 30),
    ("ny_open",        13 * 60 + 30, 15 * 60 + 30),
    ("overlap",        15 * 60 + 30, 16 * 60),
    ("ny_afternoon",   16 * 60,   20 * 60),
    ("asia_pre",       20 * 60,   24 * 60),
]

PHASES: List[str] = [name for name, _, _ in PHASE_DEFS]


def phase_name_for_minute(minute_of_day: int) -> str:
    """Phase für eine UTC-Minute des Tages (0–1439)."""
    m = minute_of_day % (24 * 60)
    for name, start, end in PHASE_DEFS:
        if start <= m < end:
            return name
    return "asia"  # unreachable — PHASE_DEFS ist lückenlos


def phase_for_ts(ts_ms: int) -> str:
    """Phase für einen Unix-Timestamp in ms (UTC)."""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return phase_name_for_minute(dt.hour * 60 + dt.minute)


# ---------------------------------------------------------------------------
# Session Profile
# ---------------------------------------------------------------------------


class SessionProfile(BaseVolumeProfile):
    def __init__(self, config: Optional[ProfileConfig] = None,
                 value_area_pct: Optional[float] = None,
                 initial_balance_minutes: Optional[int] = None,
                 poc_drift_interval_s: Optional[float] = None) -> None:
        cfg = resolve_config(config, value_area_pct=value_area_pct,
                             initial_balance_minutes=initial_balance_minutes,
                             poc_drift_interval_s=poc_drift_interval_s)
        super().__init__(config=cfg)
        cfg = self.config

        self.current_session: Optional[str] = None
        self._initial_balance_high: Optional[float] = None
        self._initial_balance_low: Optional[float] = None
        self._initial_balance_volume: float = 0.0
        self._ib_complete: bool = False
        self._last_ts_ms: Optional[int] = None

        self._ps_baseline: Optional[Dict[str, float]] = None
        self._ps_volume: Dict[str, float] = {}
        self._ps_trade_count: Dict[str, int] = {}

        # Baseline path: explicit config value or default relative to project root.
        self._ps_baseline_path: Path = (
            cfg.pre_session_baseline_path
            or Path(__file__).parent.parent / "data" / "pre_session_baseline.json"
        )

    @property
    def _trade_count_in_session(self) -> int:
        """Alias für Alt-Konsumenten (verify_pr4a_perf.py)."""
        return self._trade_count

    def _profile_label(self) -> Optional[str]:
        return self.current_session

    def ingest(self, timestamp: int, price: float, size: float, side: str) -> None:
        dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        session_name = session_name_for_hour(dt.hour)
        if session_name != self.current_session:
            self._archive_current()
            self._reset_current(session_name, timestamp)
        self._last_ts_ms = timestamp

        if is_pre_session(session_name):
            self._ps_volume[session_name] = self._ps_volume.get(session_name, 0.0) + size
            self._ps_trade_count[session_name] = self._ps_trade_count.get(session_name, 0) + 1

        if not self._ib_complete and self._start_ms is not None:
            elapsed_min = (timestamp - self._start_ms) / 60_000
            if elapsed_min <= self.config.initial_balance_minutes:
                if self._initial_balance_high is None or price > self._initial_balance_high:
                    self._initial_balance_high = price
                if self._initial_balance_low is None or price < self._initial_balance_low:
                    self._initial_balance_low = price
                self._initial_balance_volume += size
            else:
                self._ib_complete = True

        self._accumulate(timestamp, price, size)

    def _reset_current(self, session_name: str, timestamp: int) -> None:
        self.current_session = session_name
        self._reset_state(start_ms=timestamp, ts_ms=timestamp)
        self._initial_balance_high = None
        self._initial_balance_low = None
        self._initial_balance_volume = 0.0
        self._ib_complete = False
        self._last_ts_ms = timestamp

    def _load_ps_baseline(self) -> None:
        path = self._ps_baseline_path
        if path.exists():
            import json
            try:
                with open(path) as f:
                    self._ps_baseline = json.load(f)
            except Exception:
                self._ps_baseline = None

    def _save_ps_baseline(self) -> None:
        if self._ps_baseline is not None:
            import json
            self._ps_baseline_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._ps_baseline_path, "w") as f:
                json.dump(self._ps_baseline, f, indent=2)

    @staticmethod
    def build_pre_session_baseline_from_parquet(parquet_dir: Path) -> Dict[str, float]:
        import pandas as pd
        files = sorted(parquet_dir.glob("trades_*.parquet"))
        if not files:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning("No trade files found in %s for pre-session baseline.", parquet_dir)
            return {"Pre-Asia": 0.0, "Pre-London": 0.0, "Pre-NY": 0.0}

        phase_volumes: Dict[str, List[float]] = {"Pre-Asia": [], "Pre-London": [], "Pre-NY": []}
        for f in files:
            df = pd.read_parquet(f)
            if df.empty:
                continue
            hours = (df["timestamp"] // 3600_000) % 24
            for phase_name in phase_volumes:
                sd = None
                for s in SESSION_DEFS:
                    if s["name"] == phase_name:
                        sd = s
                        break
                if sd is None:
                    continue
                if sd["start"] <= sd["end"]:
                    mask = (hours >= sd["start"]) & (hours < sd["end"])
                else:
                    mask = (hours >= sd["start"]) | (hours < sd["end"])
                phase_df = df[mask]
                if not phase_df.empty:
                    phase_volumes[phase_name].append(phase_df["size"].sum())

        baseline = {}
        for phase_name in phase_volumes:
            vols = phase_volumes[phase_name]
            baseline[phase_name] = round(sum(vols) / len(vols), 4) if vols else 0.0
        return baseline

    def get_pre_session_anomaly(self, session_name: str) -> Optional[Dict[str, Any]]:
        if not is_pre_session(session_name):
            return None
        if self._ps_baseline is None:
            self._load_ps_baseline()
        if self._ps_baseline is None:
            return None
        current_vol = self._ps_volume.get(session_name, 0.0)
        baseline_vol = self._ps_baseline.get(session_name, 0.0)
        if baseline_vol <= 0:
            return None
        factor = current_vol / baseline_vol
        if factor < self.config.anomaly_factor_threshold:
            return None
        return {
            "anomaly_factor": round(factor, 2),
            "current_volume": round(current_vol, 4),
            "baseline_volume": round(baseline_vol, 4),
        }

    def current_context(self) -> Dict[str, Any]:
        if self.current_session is None:
            return {"session": "N/A"}
        ohlc = _compute_ohlc(self._prices)
        poc = _compute_poc(self._vap)
        va_h, va_l = _compute_value_area(self._vap, self.config.value_area_pct)
        current_price = self._prices[-1] if self._prices else None

        ctx: Dict[str, Any] = {
            "session": self.current_session,
            # Phase aus dem letzten Trade-Timestamp — replay-sicher,
            # keine Wall-Clock-Abhängigkeit
            "session_phase": (
                phase_for_ts(self._last_ts_ms) if self._last_ts_ms is not None else None
            ),
            "session_elapsed_seconds": (
                int(((self._last_ts_ms or 0) - (self._start_ms or 0)) / 1000)
                if self._start_ms and self._last_ts_ms else 0
            ),
            "session_poc": poc, "session_value_area_high": va_h, "session_value_area_low": va_l,
            "session_volume": round(sum(self._vap.values()), 4),
            "session_trade_count": self._trade_count,
            "session_ohlc": {"open": ohlc["open"], "high": ohlc["high"],
                             "low": ohlc["low"], "close": ohlc["close"]},
        }
        ctx["regime"] = self._compute_regime()
        if current_price is not None and poc is not None:
            ctx["price_vs_poc"] = round(current_price - poc, 2)
        if current_price is not None and va_h is not None and va_l is not None:
            ctx["price_in_value_area"] = va_l <= current_price <= va_h
        # IB auch während der Bildung sichtbar — Konsumenten unterscheiden
        # über initial_balance_complete zwischen "formt sich" und "final"
        if self._initial_balance_high is not None:
            ctx["initial_balance_high"] = self._initial_balance_high
            ctx["initial_balance_low"] = self._initial_balance_low
            ctx["initial_balance_volume"] = round(self._initial_balance_volume, 4)
            ctx["initial_balance_complete"] = self._ib_complete
        if len(self._poc_drift) >= 3:
            drift_range = max(self._poc_drift) - min(self._poc_drift)
            total_range = (max(self._prices) - min(self._prices)) if len(self._prices) > 1 else 1.0
            ctx["poc_drift_buckets"] = drift_range
            ctx["poc_drift_ratio"] = round(drift_range / total_range * self._bucket_size, 4) if total_range > 0 else 0
        return ctx

    def reset_if_needed(self, ts_ms: int) -> bool:
        """Archive and reset if ts_ms falls in a different session than current.

        Returns True if a reset occurred.
        """
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        session_name = session_name_for_hour(dt.hour)
        if session_name != self.current_session:
            self._archive_current()
            self._reset_current(session_name, ts_ms)
            return True
        return False
