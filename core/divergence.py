"""CVD Divergence Detection — compares price swings vs CVD swings.

Classification:
  - regular_bullish:     Price HH, CVD HH        (trend confirmed)
  - regular_bearish:     Price LL, CVD LL        (trend confirmed)
  - bullish_divergence:  Price LL, CVD HL        (price makes lower low, CVD makes higher low)
  - bearish_divergence:  Price HH, CVD LH        (price makes higher high, CVD makes lower high)
"""

from collections import deque
from typing import Any, Dict, List, Optional, Tuple


class DivergenceDetector:
    """Detects CVD divergences from price and CVD time series.

    Uses a fixed-lookback window. When a new bar arrives, it retroactively
    evaluates the bar at position `n - 2*lookback - 1` which now has enough
    forward context to be a confirmed swing.

    Usage:
        detector = DivergenceDetector(lookback=3)
        detector.ingest(timestamp=..., price=..., cvd_delta=...)
        divergence = detector.get_divergence()
    """

    def __init__(self, lookback: int = 3) -> None:
        self.lookback = lookback
        self._prices: deque = deque(maxlen=200)
        self._cvd: deque = deque(maxlen=200)
        self._ts: deque = deque(maxlen=200)

        self._last_high: Optional[Dict] = None
        self._last_low: Optional[Dict] = None
        self._divergence: Optional[Dict] = None
        self._history: deque = deque(maxlen=20)

        # Bestätigte Swing-Serien — Input für detect_delta_divergence()
        self._swing_highs: deque = deque(maxlen=20)
        self._swing_lows: deque = deque(maxlen=20)

    def ingest(self, ts: int, price: float, cvd_delta: float) -> None:
        self._prices.append(price)
        self._cvd.append(cvd_delta)
        self._ts.append(ts)

        n = len(self._prices)
        if n < 2 * self.lookback + 1:
            return

        # The point we can retroactively confirm: lookback bars before the newest,
        # with lookback bars after it.
        mid = n - self.lookback - 1
        if mid < self.lookback:
            return

        p = list(self._prices)
        c = list(self._cvd)
        t = list(self._ts)

        lo = mid - self.lookback
        hi = mid + self.lookback + 1

        # Swing high?
        if all(p[mid] >= p[i] for i in range(lo, hi)):
            # Also check: must be a local extremum (not flat)
            left_higher = any(p[i] > p[mid] for i in range(mid - 1, lo - 1, -1))
            if not left_higher:
                right_higher = any(p[i] > p[mid] for i in range(mid + 1, hi))
                if not right_higher:
                    # Confirmed swing high
                    self._register_high(t[mid], p[mid], c[mid])

        # Swing low?
        if all(p[mid] <= p[i] for i in range(lo, hi)):
            left_lower = any(p[i] < p[mid] for i in range(mid - 1, lo - 1, -1))
            if not left_lower:
                right_lower = any(p[i] < p[mid] for i in range(mid + 1, hi))
                if not right_lower:
                    self._register_low(t[mid], p[mid], c[mid])

    def _register_high(self, ts: int, price: float, cvd: float) -> None:
        if self._last_high is not None:
            if price > self._last_high["price"]:
                div = "bearish_divergence" if cvd < self._last_high["cvd"] else "regular_bullish"
                self._emit(div, ts, price, cvd, self._last_high)
        self._last_high = {"ts": ts, "price": price, "cvd": cvd}
        self._swing_highs.append({"ts": ts, "price": price, "cvd": cvd})

    def _register_low(self, ts: int, price: float, cvd: float) -> None:
        if self._last_low is not None:
            if price < self._last_low["price"]:
                div = "bullish_divergence" if cvd > self._last_low["cvd"] else "regular_bearish"
                self._emit(div, ts, price, cvd, self._last_low)
        self._last_low = {"ts": ts, "price": price, "cvd": cvd}
        self._swing_lows.append({"ts": ts, "price": price, "cvd": cvd})

    def get_swing_highs(self) -> List[Dict]:
        """Bestätigte Swing Highs (ts, price, cvd) — ältestes zuerst."""
        return list(self._swing_highs)

    def get_swing_lows(self) -> List[Dict]:
        """Bestätigte Swing Lows (ts, price, cvd) — ältestes zuerst."""
        return list(self._swing_lows)

    def _emit(self, div_type: str, ts: int, price: float, cvd: float, prev: Dict) -> None:
        event = {
            "divergence_type": div_type, "timestamp": ts,
            "price": price, "cvd_delta": cvd,
            "prev_price": prev["price"], "prev_cvd": prev["cvd"],
        }
        self._divergence = event
        self._history.append(event)

    @property
    def current_divergence(self) -> Optional[Dict]:
        d = self._divergence
        self._divergence = None
        return d

    def get_divergence_history(self) -> List[Dict]:
        return list(self._history)
