"""Session Profile — OHLC, VAP, POC, Value Area, Initial Balance, POC drift.

Sessions are UTC-based windows with explicit Pre-Session phases:

  Asia         00:00-08:00
  Pre-London   06:00-08:00  (overlap with Asia tail)
  London       08:00-13:00
  Pre-NY       12:00-13:00  (overlap with London tail)
  New York     13:00-21:00
  Pre-Asia     21:00-00:00  (overlap with NY tail)

Phase transitions archive the current profile and start a fresh one.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BUCKET = 25

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
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ProfileSnapshot:
    label: str
    timestamp: int
    ohlc: Dict[str, Optional[float]]
    poc: Optional[int]
    value_area_high: Optional[int]
    value_area_low: Optional[int]
    total_volume: float
    bucket_count: int
    vap: Dict[int, float]
    poc_drift: List[int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label, "timestamp": self.timestamp,
            "open": self.ohlc.get("open"), "high": self.ohlc.get("high"),
            "low": self.ohlc.get("low"), "close": self.ohlc.get("close"),
            "poc": self.poc, "value_area_high": self.value_area_high,
            "value_area_low": self.value_area_low,
            "total_volume": self.total_volume, "bucket_count": self.bucket_count,
        }


# ---------------------------------------------------------------------------
# VAP helpers
# ---------------------------------------------------------------------------

def _bucket(price: float) -> int:
    return int(price / BUCKET) * BUCKET


def _compute_ohlc(prices: List[float]) -> Dict[str, Optional[float]]:
    if not prices:
        return {"open": None, "high": None, "low": None, "close": None}
    return {"open": prices[0], "high": max(prices), "low": min(prices), "close": prices[-1]}


def _compute_poc(vap: Dict[int, float]) -> Optional[int]:
    if not vap:
        return None
    return max(vap, key=vap.get)


def _compute_value_area(vap: Dict[int, float], target_pct: float = 0.7) -> Tuple[Optional[int], Optional[int]]:
    """Return (va_high, va_low) — high first for caller convenience."""
    if not vap:
        return None, None
    poc = _compute_poc(vap)
    if poc is None:
        return None, None
    total = sum(vap.values())
    if total == 0:
        return None, None
    buckets = sorted(vap.keys())
    poc_idx = buckets.index(poc) if poc in buckets else 0
    cum_vol = vap[poc]
    low_idx = poc_idx
    high_idx = poc_idx
    while cum_vol / total < target_pct:
        expanded = False
        if low_idx > 0:
            low_idx -= 1
            cum_vol += vap[buckets[low_idx]]
            expanded = True
        if high_idx < len(buckets) - 1 and cum_vol / total < target_pct:
            high_idx += 1
            cum_vol += vap[buckets[high_idx]]
            expanded = True
        if not expanded:
            break
    # Return (high, low)
    return buckets[high_idx], buckets[low_idx]


# ---------------------------------------------------------------------------
# Session Profile
# ---------------------------------------------------------------------------

PRE_SESSION_BASELINE_PATH = Path(__file__).parent.parent / "data" / "pre_session_baseline.json"


class SessionProfile:
    def __init__(self, value_area_pct: float = 0.7, initial_balance_minutes: int = 60,
                 poc_drift_interval_s: float = 60.0) -> None:
        self.value_area_pct = value_area_pct
        self.initial_balance_minutes = initial_balance_minutes
        self.poc_drift_interval_s = poc_drift_interval_s

        self.current_session: Optional[str] = None
        self._prices: List[float] = []
        self._vap: Dict[int, float] = {}
        self._session_start_ms: Optional[int] = None
        self._initial_balance_high: Optional[float] = None
        self._initial_balance_low: Optional[float] = None
        self._initial_balance_volume: float = 0.0
        self._ib_complete: bool = False
        self._last_poc_snapshot_ms: int = 0
        self._poc_drift: List[int] = []
        self._trade_count_in_session: int = 0
        self._price_min: Optional[float] = None
        self._price_max: Optional[float] = None

        self._archived: deque = deque(maxlen=60)

        self._ps_baseline: Optional[Dict[str, float]] = None
        self._ps_volume: Dict[str, float] = {}
        self._ps_trade_count: Dict[str, int] = {}
        self.anomaly_factor_threshold: float = 1.5

    def ingest(self, timestamp: int, price: float, size: float, side: str) -> None:
        dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        session_name = session_name_for_hour(dt.hour)
        if session_name != self.current_session:
            self._archive_current()
            self._reset_current(session_name, timestamp)

        if is_pre_session(session_name):
            self._ps_volume[session_name] = self._ps_volume.get(session_name, 0.0) + size
            self._ps_trade_count[session_name] = self._ps_trade_count.get(session_name, 0) + 1

        bk = _bucket(price)
        self._vap[bk] = self._vap.get(bk, 0.0) + size
        self._prices.append(price)
        self._trade_count_in_session += 1
        if self._price_min is None or price < self._price_min:
            self._price_min = price
        if self._price_max is None or price > self._price_max:
            self._price_max = price

        if not self._ib_complete and self._session_start_ms is not None:
            elapsed_min = (timestamp - self._session_start_ms) / 60_000
            if elapsed_min <= self.initial_balance_minutes:
                if self._initial_balance_high is None or price > self._initial_balance_high:
                    self._initial_balance_high = price
                if self._initial_balance_low is None or price < self._initial_balance_low:
                    self._initial_balance_low = price
                self._initial_balance_volume += size
            else:
                self._ib_complete = True

        if timestamp - self._last_poc_snapshot_ms >= self.poc_drift_interval_s * 1000:
            poc = _compute_poc(self._vap)
            if poc is not None:
                self._poc_drift.append(poc)
            self._last_poc_snapshot_ms = timestamp

    def _reset_current(self, session_name: str, timestamp: int) -> None:
        self.current_session = session_name
        self._prices = []
        self._vap = {}
        self._session_start_ms = timestamp
        self._initial_balance_high = None
        self._initial_balance_low = None
        self._initial_balance_volume = 0.0
        self._ib_complete = False
        self._last_poc_snapshot_ms = timestamp
        self._poc_drift = []
        self._trade_count_in_session = 0
        self._price_min = None
        self._price_max = None

    def _compute_regime(self) -> str:
        """Balanced/imbalanced heuristic based on value-area width vs price range."""
        if not self._vap or not self._prices:
            return "neutral"
        va_h, va_l = _compute_value_area(self._vap, self.value_area_pct)
        if va_h is None or va_l is None:
            return "neutral"
        # Use bucket of current price so comparison stays on the same grid as VA
        current_bucket = _bucket(self._prices[-1])
        if current_bucket > va_h:
            return "imbalanced_up"
        if current_bucket < va_l:
            return "imbalanced_down"
        p_min = self._price_min if self._price_min is not None else min(self._prices)
        p_max = self._price_max if self._price_max is not None else max(self._prices)
        price_range = p_max - p_min
        if price_range == 0:
            return "balanced"
        return "balanced" if (va_h - va_l) / price_range >= 0.5 else "imbalanced"

    def _archive_current(self) -> None:
        if self.current_session is None or not self._prices:
            return
        ohlc = _compute_ohlc(self._prices)
        poc = _compute_poc(self._vap)
        va_h, va_l = _compute_value_area(self._vap, self.value_area_pct)
        snap = ProfileSnapshot(
            label=self.current_session, timestamp=int(datetime.now(timezone.utc).timestamp() * 1000),
            ohlc=ohlc, poc=poc, value_area_high=va_h, value_area_low=va_l,
            total_volume=sum(self._vap.values()), bucket_count=len(self._vap),
            vap=dict(self._vap), poc_drift=list(self._poc_drift),
        )
        self._archived.append(snap)

    def _load_ps_baseline(self) -> None:
        path = PRE_SESSION_BASELINE_PATH
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
            PRE_SESSION_BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(PRE_SESSION_BASELINE_PATH, "w") as f:
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
        if factor < self.anomaly_factor_threshold:
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
        va_h, va_l = _compute_value_area(self._vap, self.value_area_pct)
        current_price = self._prices[-1] if self._prices else None

        ctx: Dict[str, Any] = {
            "session": self.current_session,
            "session_elapsed_seconds": (
                int((datetime.now(timezone.utc).timestamp() * 1000 - (self._session_start_ms or 0)) / 1000)
                if self._session_start_ms else 0
            ),
            "session_poc": poc, "session_value_area_high": va_h, "session_value_area_low": va_l,
            "session_volume": round(sum(self._vap.values()), 4),
            "session_trade_count": self._trade_count_in_session,
            "session_ohlc": {"open": ohlc["open"], "high": ohlc["high"],
                             "low": ohlc["low"], "close": ohlc["close"]},
        }
        ctx["regime"] = self._compute_regime()
        if current_price is not None and poc is not None:
            ctx["price_vs_poc"] = round(current_price - poc, 2)
        if current_price is not None and va_h is not None and va_l is not None:
            ctx["price_in_value_area"] = va_l <= current_price <= va_h
        if self._ib_complete:
            ctx["initial_balance_high"] = self._initial_balance_high
            ctx["initial_balance_low"] = self._initial_balance_low
            ctx["initial_balance_volume"] = round(self._initial_balance_volume, 4)
        if len(self._poc_drift) >= 3:
            drift_range = max(self._poc_drift) - min(self._poc_drift)
            total_range = (max(self._prices) - min(self._prices)) if len(self._prices) > 1 else 1.0
            ctx["poc_drift_buckets"] = drift_range
            ctx["poc_drift_ratio"] = round(drift_range / total_range * BUCKET, 4) if total_range > 0 else 0
        return ctx

    def ingest_trade(self, price: float, size: float, ts_ms: int) -> None:
        """Simplified trade ingestion without side (side unused for VAP computation)."""
        self.ingest(ts_ms, price, size, "buy")

    def snapshot(self) -> dict:
        """Return current context snapshot (canonical PR4a interface)."""
        return self.current_context()

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

    def get_archived_profiles(self) -> List[ProfileSnapshot]:
        return list(self._archived)
