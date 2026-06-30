"""Weekly Profile — OHLC, VAP, POC, Value Area for the crypto trading week.

Reset: Sunday 22:00 UTC (crypto trades 24/7; this is a fixed weekly anchor).

Archived profiles are stored alongside session profiles (shared deque) but
with a label prefix "Week-YYYY-Www" for weekly aggregation in composite_profile.py.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.session_profile import (
    ProfileSnapshot, _bucket, _compute_ohlc, _compute_poc, _compute_value_area
)

WEEKLY_RESET_HOUR = 22  # Sunday 22:00 UTC
WEEKLY_RESET_DAY = 6     # Sunday = 6 in Python weekday() (Mon=0 ... Sun=6)

DEFAULT_VALUE_AREA_PCT = 0.7


def _week_start_ms(timestamp: int) -> Tuple[int, str]:
    """Return the start-of-week timestamp (Sunday 22:00 UTC) and ISO week label
    for a given timestamp in ms.

    This is a module-level function (not a staticmethod) to avoid being shadowed
    by an instance attribute with the same name.
    """
    dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
    days_since_sunday = (dt.weekday() + 1) % 7
    if days_since_sunday > 0 or dt.hour < WEEKLY_RESET_HOUR:
        days_to_subtract = days_since_sunday if days_since_sunday > 0 else 7
        start_dt = dt - timedelta(days=days_to_subtract)
        start_dt = start_dt.replace(hour=WEEKLY_RESET_HOUR, minute=0, second=0, microsecond=0)
    else:
        start_dt = dt.replace(hour=WEEKLY_RESET_HOUR, minute=0, second=0, microsecond=0)

    start_ms = int(start_dt.timestamp() * 1000)
    ref_dt = start_dt + timedelta(days=2)
    label = f"Week-{ref_dt.strftime('%Y-W%V')}"
    return start_ms, label


class WeeklyProfile:
    """Live weekly profile builder.

    Usage:
        wp = WeeklyProfile()
        wp.ingest(timestamp=..., price=..., size=..., side=...)
        ctx = wp.current_context()
    """

    def __init__(self, value_area_pct: float = DEFAULT_VALUE_AREA_PCT,
                 poc_drift_interval_s: float = 300.0) -> None:
        self.value_area_pct = value_area_pct
        self.poc_drift_interval_s = poc_drift_interval_s

        self._prices: List[float] = []
        self._vap: Dict[int, float] = {}
        self._start_ms: Optional[int] = None
        self._label: Optional[str] = None
        self._trade_count: int = 0
        self._last_poc_snapshot_ms: int = 0
        self._poc_drift: List[int] = []

        # Archived weekly profiles
        self._archived: deque = deque(maxlen=52)

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest(self, timestamp: int, price: float, size: float, side: str) -> None:
        """Process a single trade."""
        ws_ms, ws_label = _week_start_ms(timestamp)

        if ws_label != self._label:
            self._archive_current()
            self._prices = []
            self._vap = {}
            self._start_ms = ws_ms
            self._label = ws_label
            self._trade_count = 0
            self._last_poc_snapshot_ms = timestamp
            self._poc_drift = []

        bk = _bucket(price)
        self._vap[bk] = self._vap.get(bk, 0.0) + size
        self._prices.append(price)
        self._trade_count += 1

        if timestamp - self._last_poc_snapshot_ms >= self.poc_drift_interval_s * 1000:
            poc = _compute_poc(self._vap)
            if poc is not None:
                self._poc_drift.append(poc)
            self._last_poc_snapshot_ms = timestamp

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _archive_current(self) -> None:
        if self._label is None or not self._prices:
            return

        ohlc = _compute_ohlc(self._prices)
        poc = _compute_poc(self._vap)
        va_h, va_l = _compute_value_area(self._vap, self.value_area_pct)

        snap = ProfileSnapshot(
            label=self._label,
            timestamp=int(datetime.now(timezone.utc).timestamp() * 1000),
            ohlc=ohlc,
            poc=poc,
            value_area_high=va_h,
            value_area_low=va_l,
            total_volume=sum(self._vap.values()),
            bucket_count=len(self._vap),
            vap=dict(self._vap),
            poc_drift=list(self._poc_drift),
        )
        self._archived.append(snap)

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def current_context(self) -> Dict[str, Any]:
        if self._label is None:
            return {"week": "N/A"}

        ohlc = _compute_ohlc(self._prices)
        poc = _compute_poc(self._vap)
        va_h, va_l = _compute_value_area(self._vap, self.value_area_pct)
        current_price = self._prices[-1] if self._prices else None

        ctx: Dict[str, Any] = {
            "week": self._label,
            "week_poc": poc,
            "week_value_area_high": va_h,
            "week_value_area_low": va_l,
            "week_volume": round(sum(self._vap.values()), 4),
            "week_ohlc": {
                "open": ohlc["open"],
                "high": ohlc["high"],
                "low": ohlc["low"],
                "close": ohlc["close"],
            },
        }

        if current_price is not None:
            ctx["week_price"] = current_price
            if poc is not None:
                ctx["price_vs_week_poc"] = round(current_price - poc, 2)
            if va_h is not None and va_l is not None:
                ctx["price_in_week_value_area"] = va_l <= current_price <= va_h

        return ctx

    def get_archived_profiles(self) -> List[ProfileSnapshot]:
        return list(self._archived)
