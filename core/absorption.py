"""Realtime absorption detection from trade stream + CVD + price movement.

Logic:
  1. Track cumulative volume within a rolling time window (default 30-60s).
  2. If high volume in a narrow price band AND small price movement (< Y bps),
     flag as absorption candidate.
  3. Combined with CVD direction:
     - Strong negative delta, no price decline  -> absorption of selling pressure (bullish)
     - Strong positive delta, no price rise     -> absorption of buying pressure (bearish)

Output: Event dict with price level, direction, strength (relative volume vs baseline).
"""

from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.pattern_engine import BUCKET

DEFAULT_WINDOW_SECONDS = 45
DEFAULT_PRICE_MOVE_BPS = 2.0    # max price move for absorption candidate
DEFAULT_STRENGTH_THRESHOLD = 1.5  # multiple of period-average volume


class AbsorptionDetector:
    """Detects absorption events from trade stream + CVD data.

    Usage:
        detector = AbsorptionDetector()
        event = detector.ingest(trade={"price": ..., "size": ..., "side": ...},
                                cvd_snapshot={...}, current_price=...)
        if event:  # absorption detected
            print(event)
    """

    def __init__(self, window_seconds: float = DEFAULT_WINDOW_SECONDS,
                 max_price_move_bps: float = DEFAULT_PRICE_MOVE_BPS,
                 strength_threshold: float = DEFAULT_STRENGTH_THRESHOLD) -> None:
        self.window_seconds = window_seconds
        self.max_price_move_bps = max_price_move_bps
        self.strength_threshold = strength_threshold

        # Rolling trade buffer: (timestamp_ms, price, size, side)
        self._trades: deque = deque()

        # Running window stats
        self._window_volume: float = 0.0
        self._window_buy_volume: float = 0.0
        self._window_sell_volume: float = 0.0
        self._window_min_price: Optional[float] = None
        self._window_max_price: Optional[float] = None

        # Longer-term volume baseline for strength comparison
        self._long_volume_buffer: deque = deque(maxlen=600)  # last 10 min at 1s buckets
        self._last_drain_ms: int = 0

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest(self, trade: Dict[str, Any],
               cvd_snapshot: Optional[Dict[str, Any]] = None,
               current_price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Process a single trade. Returns absorption event dict if detected.

        Args:
            trade: dict with keys price (float), size (float), side (str), timestamp (int ms).
            cvd_snapshot: Optional dict from cvd.snapshot() for divergence check.
            current_price: Optional current mid-price for bps calculation.

        Returns:
            None or event dict with keys:
              - absorption_type: str ("bullish" = sell absorbed, "bearish" = buy absorbed)
              - price_level: float (the current/event price)
              - volume: float (total volume in window)
              - strength: float (multiple of baseline volume)
              - delta: float (net CVD delta in window)
              - price_move_bps: float (price change in bps)
              - timestamp: int (event timestamp ms)
        """
        ts = trade.get("timestamp", 0)

        # Prune old trades from window
        self._prune(ts)

        # Add this trade
        price = float(trade.get("price", 0))
        size = float(trade.get("size", 0))
        side = str(trade.get("side", ""))

        self._trades.append((ts, price, size, side))
        self._window_volume += size
        if side == "buy":
            self._window_buy_volume += size
        else:
            self._window_sell_volume += size

        if self._window_min_price is None or price < self._window_min_price:
            self._window_min_price = price
        if self._window_max_price is None or price > self._window_max_price:
            self._window_max_price = price

        # Check conditions
        return self._evaluate(ts, current_price, cvd_snapshot)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _prune(self, now_ms: int) -> None:
        """Remove trades older than the window from rolling buffer."""
        cutoff = now_ms - int(self.window_seconds * 1000)
        while self._trades and self._trades[0][0] < cutoff:
            old_ts, old_p, old_s, old_side = self._trades.popleft()
            self._window_volume -= old_s
            if old_side == "buy":
                self._window_buy_volume -= old_s
            else:
                self._window_sell_volume -= old_s

        # Recalculate min/max price from remaining trades
        if self._trades:
            prices = [t[1] for t in self._trades]
            self._window_min_price = min(prices)
            self._window_max_price = max(prices)
        else:
            self._window_min_price = None
            self._window_max_price = None

    def _evaluate(self, now_ms: int,
                  current_price: Optional[float],
                  cvd_snapshot: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Check if current window qualifies as absorption."""
        if self._window_volume <= 0:
            return None

        # Price movement check
        if self._window_min_price is not None and self._window_max_price is not None:
            avg_price = (self._window_min_price + self._window_max_price) / 2.0
            price_range_bps = ((self._window_max_price - self._window_min_price) / avg_price) * 10_000_000 if avg_price > 0 else 0
        else:
            price_range_bps = 0.0

        if price_range_bps > self.max_price_move_bps:
            return None  # Price moved too much — not absorption

        # Get net delta from CVD snapshot (or compute from our window)
        if cvd_snapshot is not None:
            delta = cvd_snapshot.get("rolling_delta", 0.0)
        else:
            delta = self._window_buy_volume - self._window_sell_volume

        # Determine direction
        abs_delta = abs(delta)
        if abs_delta < self._window_volume * 0.1:
            # Very balanced — not enough directional flow to call absorption
            return None

        # Strength vs baseline (long-term volume per window)
        baseline_vol = self._estimate_baseline_volume()
        strength = self._window_volume / baseline_vol if baseline_vol > 0 else 1.0
        if strength < self.strength_threshold:
            return None

        # Classify
        if delta < 0:
            # Negative delta (more sells), price held -> bullish absorption
            absorption_type = "bullish"
        else:
            # Positive delta (more buys), price held -> bearish absorption
            absorption_type = "bearish"

        event_price = current_price or self._window_max_price or self._window_min_price or 0.0

        return {
            "absorption_type": absorption_type,
            "price_level": round(event_price, 2),
            "volume": round(self._window_volume, 4),
            "strength": round(strength, 2),
            "delta": round(delta, 4),
            "price_move_bps": round(price_range_bps, 2),
            "timestamp": now_ms,
        }

    def _estimate_baseline_volume(self) -> float:
        """Estimate average volume per window from longer-term history."""
        if not self._long_volume_buffer:
            return self._window_volume if self._window_volume > 0 else 1.0
        return sum(self._long_volume_buffer) / len(self._long_volume_buffer)

    def record_window_volume(self, volume: float) -> None:
        """Record total volume of a completed window for baseline estimation.

        Called externally once per window period (e.g. by aggregator_agent).
        """
        self._long_volume_buffer.append(volume)
