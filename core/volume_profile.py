"""Volume Profile Basis — gemeinsame VAP/POC/Value-Area/Regime-Maschinerie.

Sprint B Konsolidierung: SessionProfile und WeeklyProfile trugen zuvor je
eine identische Kopie dieser Logik (VAP-Akkumulation, POC, Value Area,
Regime-Heuristik, POC-Drift, Archivierung). Hier lebt sie genau einmal;
die Subklassen liefern nur noch Reset-Grenzen (Session-Wechsel vs.
Wochen-Anker) und ihre Kontext-Feldnamen.

Alle Schwellwerte kommen aus ProfileConfig (core/profile_config.py) —
keine Magic Numbers im Modul-Code.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.profile_config import ProfileConfig, PROFILE_CONFIG, resolve_config

# Backward-compat alias — used by tests and external consumers.
# Prefer config.bucket_size in new code.
BUCKET = PROFILE_CONFIG.bucket_size


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

def _bucket(price: float, bucket_size: int = BUCKET) -> int:
    """Round price down to nearest bucket boundary."""
    return int(price / bucket_size) * bucket_size


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
# Base class
# ---------------------------------------------------------------------------

class BaseVolumeProfile:
    """Gemeinsamer Zustand + Berechnungen für Session- und Weekly-Profile.

    Subklassen implementieren:
      - ingest(timestamp, price, size, side): Grenz-Check, dann _accumulate()
      - _profile_label(): Label für Archiv-Snapshots (None = nichts zu archivieren)
      - current_context(): Kontext-Dict mit den jeweiligen Feldnamen

    Alle Schwellwerte kommen aus ProfileConfig. Ohne explizites config-Argument
    wird PROFILE_CONFIG (die Default-Instanz) verwendet. Einzelne Felder
    koennen weiterhin per Keyword ueberschrieben werden (Rueckwaertskompatibel
    zu den alten Konstruktor-Signaturen vor der ProfileConfig-Einfuehrung).
    """

    def __init__(self, config: Optional[ProfileConfig] = None,
                 value_area_pct: Optional[float] = None,
                 poc_drift_interval_s: Optional[float] = None,
                 archive_maxlen: Optional[int] = None) -> None:
        cfg = resolve_config(config, value_area_pct=value_area_pct,
                             poc_drift_interval_s=poc_drift_interval_s,
                             archive_maxlen=archive_maxlen)
        self.config = cfg
        self._bucket_size = cfg.bucket_size

        self._prices: List[float] = []
        self._vap: Dict[int, float] = {}
        self._start_ms: Optional[int] = None
        self._trade_count: int = 0
        self._last_poc_snapshot_ms: int = 0
        self._poc_drift: List[int] = []
        self._price_min: Optional[float] = None
        self._price_max: Optional[float] = None

        self._archived: deque = deque(maxlen=cfg.archive_maxlen)
        # Monotonic lifetime counter — unlike len(self._archived), this never
        # decreases when the ring buffer evicts old entries. Consumers that
        # need to detect "a new profile was archived" (e.g. zone rebuild
        # cache invalidation) must use this, not len(get_archived_profiles()).
        self._archive_total_count: int = 0

    # -- Akkumulation ---------------------------------------------------

    def _accumulate(self, timestamp: int, price: float, size: float) -> None:
        """VAP, Preis-Serie, Min/Max, Trade-Zähler und POC-Drift fortschreiben."""
        bk = _bucket(price, self._bucket_size)
        self._vap[bk] = self._vap.get(bk, 0.0) + size
        self._prices.append(price)
        self._trade_count += 1
        if self._price_min is None or price < self._price_min:
            self._price_min = price
        if self._price_max is None or price > self._price_max:
            self._price_max = price

        if timestamp - self._last_poc_snapshot_ms >= self.config.poc_drift_interval_s * 1000:
            poc = _compute_poc(self._vap)
            if poc is not None:
                self._poc_drift.append(poc)
            self._last_poc_snapshot_ms = timestamp

    def _reset_state(self, start_ms: int, ts_ms: int) -> None:
        self._prices = []
        self._vap = {}
        self._start_ms = start_ms
        self._trade_count = 0
        self._last_poc_snapshot_ms = ts_ms
        self._poc_drift = []
        self._price_min = None
        self._price_max = None

    # -- Regime ----------------------------------------------------------

    def _compute_regime(self) -> str:
        """Balanced/imbalanced heuristic based on value-area width vs price range."""
        if not self._vap or not self._prices:
            return "neutral"
        va_h, va_l = _compute_value_area(self._vap, self.config.value_area_pct)
        if va_h is None or va_l is None:
            return "neutral"
        # Use bucket of current price so comparison stays on the same grid as VA
        current_bucket = _bucket(self._prices[-1], self._bucket_size)
        if current_bucket > va_h:
            return "imbalanced_up"
        if current_bucket < va_l:
            return "imbalanced_down"
        p_min = self._price_min if self._price_min is not None else min(self._prices)
        p_max = self._price_max if self._price_max is not None else max(self._prices)
        price_range = p_max - p_min
        if price_range == 0:
            return "balanced"
        return "balanced" if (va_h - va_l) / price_range >= self.config.regime_balanced_ratio else "imbalanced"

    # -- Archiv ------------------------------------------------------------

    def _profile_label(self) -> Optional[str]:
        raise NotImplementedError

    def _archive_current(self) -> None:
        label = self._profile_label()
        if label is None or not self._prices:
            return
        ohlc = _compute_ohlc(self._prices)
        poc = _compute_poc(self._vap)
        va_h, va_l = _compute_value_area(self._vap, self.config.value_area_pct)
        snap = ProfileSnapshot(
            label=label,
            timestamp=int(datetime.now(timezone.utc).timestamp() * 1000),
            ohlc=ohlc, poc=poc, value_area_high=va_h, value_area_low=va_l,
            total_volume=sum(self._vap.values()), bucket_count=len(self._vap),
            vap=dict(self._vap), poc_drift=list(self._poc_drift),
        )
        self._archived.append(snap)
        self._archive_total_count += 1

    def get_archived_profiles(self) -> List[ProfileSnapshot]:
        return list(self._archived)

    @property
    def archived_total_count(self) -> int:
        """Lifetime count of archived profiles — monotonic, survives ring-buffer
        eviction. Use this (not len(get_archived_profiles())) to detect
        "a new profile was archived" once the archive has filled up."""
        return self._archive_total_count

    # -- Kanonisches PR4a-Interface -----------------------------------------

    def current_context(self) -> Dict[str, Any]:
        raise NotImplementedError

    def ingest(self, timestamp: int, price: float, size: float, side: str) -> None:
        raise NotImplementedError

    def ingest_trade(self, price: float, size: float, ts_ms: int) -> None:
        """Simplified trade ingestion without side (side unused for VAP computation)."""
        self.ingest(ts_ms, price, size, "buy")

    def snapshot(self) -> dict:
        """Return current context snapshot (canonical PR4a interface)."""
        return self.current_context()
