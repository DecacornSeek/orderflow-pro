"""Weekly Profile — OHLC, VAP, POC, Value Area for the crypto trading week.

Reset: Sunday 22:00 UTC (crypto trades 24/7; this is a fixed weekly anchor).

Archived profiles are stored alongside session profiles (shared deque) but
with a label prefix "Week-YYYY-Www" for weekly aggregation in composite_profile.py.

Die gemeinsame Profil-Maschinerie (VAP/POC/VA/Regime/Archiv) lebt seit
Sprint B in core/volume_profile.py — hier nur der Wochen-Anker.

Alle Schwellwerte kommen aus ProfileConfig.
"""

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

from core.profile_config import ProfileConfig, PROFILE_CONFIG, resolve_config
from core.volume_profile import (
    BaseVolumeProfile, _compute_ohlc, _compute_poc, _compute_value_area,
)

WEEKLY_RESET_HOUR = 22  # Sunday 22:00 UTC
WEEKLY_RESET_DAY = 6     # Sunday = 6 in Python weekday() (Mon=0 ... Sun=6)


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


class WeeklyProfile(BaseVolumeProfile):
    """Live weekly profile builder.

    Usage:
        wp = WeeklyProfile()
        wp.ingest(timestamp=..., price=..., size=..., side=...)
        ctx = wp.current_context()
    """

    def __init__(self, config: Optional[ProfileConfig] = None,
                 value_area_pct: Optional[float] = None,
                 poc_drift_interval_s: Optional[float] = None) -> None:
        cfg = resolve_config(config, value_area_pct=value_area_pct,
                             poc_drift_interval_s=poc_drift_interval_s)
        super().__init__(config=cfg)
        self._label: Optional[str] = None

    def _profile_label(self) -> Optional[str]:
        return self._label

    def _reset_week(self, ws_ms: int, ws_label: str, ts_ms: int) -> None:
        self._label = ws_label
        self._reset_state(start_ms=ws_ms, ts_ms=ts_ms)

    def ingest(self, timestamp: int, price: float, size: float, side: str) -> None:
        """Process a single trade."""
        ws_ms, ws_label = _week_start_ms(timestamp)

        if ws_label != self._label:
            self._archive_current()
            self._reset_week(ws_ms, ws_label, timestamp)

        self._accumulate(timestamp, price, size)

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def current_context(self) -> Dict[str, Any]:
        if self._label is None:
            return {"week": "N/A"}

        ohlc = _compute_ohlc(self._prices)
        poc = _compute_poc(self._vap)
        va_h, va_l = _compute_value_area(self._vap, self.config.value_area_pct)
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

        ctx["regime"] = self._compute_regime()
        if current_price is not None:
            ctx["week_price"] = current_price
            if poc is not None:
                ctx["price_vs_week_poc"] = round(current_price - poc, 2)
            if va_h is not None and va_l is not None:
                ctx["price_in_week_value_area"] = va_l <= current_price <= va_h

        return ctx

    def reset_if_needed(self, ts_ms: int) -> bool:
        """Archive and reset if ts_ms falls in a different week than current.

        Returns True if a reset occurred.
        """
        ws_ms, ws_label = _week_start_ms(ts_ms)
        if ws_label != self._label:
            self._archive_current()
            self._reset_week(ws_ms, ws_label, ts_ms)
            return True
        return False
