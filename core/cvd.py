"""
CVD (Cumulative Volume Delta) - Rolling Delta + CVD State.

Subscriber (Aggregator Agent) ruft für jeden Trade die update()-Methode auf.
Das CVD-Modul pflegt:
  - rolling_delta:  Netto-Delta über das konfigurierte Fenster (z.B. 200 Trades)
  - cumulative_cvd: Summe der absoluten Deltas (total volume)
  - buy_volume / sell_volume:  Rohvolumen pro Seite
  - window:  Liste der letzten N Trades (für rolling window)

Alle Werte sind über snapshot() serialisierbar → Redis Pub/Message.
"""

import logging
from collections import deque
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class CVD:
    """Rolling CVD state - thread-safe wenn nur aus einer asyncio Task aufgerufen.

    Args:
        window_size: Anzahl der Trades, die im Rolling-Delta-Fenster bleiben.
    """

    def __init__(self, window_size: int = 200) -> None:
        self.window_size = window_size

        # Kumulierte Werte (seit Start / letztem Reset)
        self.cumulative_buy_volume: float = 0.0
        self.cumulative_sell_volume: float = 0.0
        self.cumulative_delta: float = 0.0  # buy - sell

        # Rolling Fenster (manuelle Verwaltung ohne maxlen, damit wir
        # den evicted Trade abziehen können)
        self._window: deque = deque()  # kein maxlen!
        self._rolling_delta: float = 0.0
        self._rolling_buy: float = 0.0
        self._rolling_sell: float = 0.0

        # Metadaten
        self.trade_count: int = 0
        self.last_price: Optional[float] = None
        self.last_timestamp: Optional[int] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, price: float, size: float, aggressor_side: str,
               timestamp: Optional[int] = None) -> Dict[str, Any]:
        """Einen Trade verarbeiten und aktualisierte Metriken zurückgeben.

        Args:
            price: Trade-Preis.
            size:  Handelsvolumen (in Base Currency).
            aggressor_side: 'buy' oder 'sell'.
            timestamp: Unix ms (optional).

        Returns:
            snapshot der aktuellen Metriken (siehe snapshot()).
        """
        aggressor_side = str(aggressor_side).lower()
        if aggressor_side not in ("buy", "sell"):
            logger.warning("CVD.update: unrecognised aggressor_side %r, treating as sell", aggressor_side)
            aggressor_side = "sell"

        delta = size if aggressor_side == "buy" else -size

        # Kumuliert
        if aggressor_side == "buy":
            self.cumulative_buy_volume += size
        else:
            self.cumulative_sell_volume += size
        self.cumulative_delta += delta

        # Rolling Fenster: evicted Trade abziehen BEVOR wir den neuen anfügen
        if len(self._window) == self.window_size:
            oldest = self._window.popleft()
            self._rolling_delta -= oldest[3]  # oldest delta
            if oldest[2] == "buy":
                self._rolling_buy -= oldest[1]
            else:
                self._rolling_sell -= oldest[1]

        # Neuen Trade anfügen
        self._window.append((price, size, aggressor_side, delta, timestamp))
        self._rolling_delta += delta
        if aggressor_side == "buy":
            self._rolling_buy += size
        else:
            self._rolling_sell += size

        self.trade_count += 1
        self.last_price = price
        if timestamp is not None:
            self.last_timestamp = timestamp

        return self.snapshot()

    def reset(self) -> None:
        """Alle kumulierten und Rolling-Werte zurücksetzen."""
        self.cumulative_buy_volume = 0.0
        self.cumulative_sell_volume = 0.0
        self.cumulative_delta = 0.0
        self._window.clear()
        self._rolling_delta = 0.0
        self._rolling_buy = 0.0
        self._rolling_sell = 0.0
        self.trade_count = 0
        self.last_price = None
        self.last_timestamp = None

    def snapshot(self) -> Dict[str, Any]:
        """Serialisierbaren Snapshot aller CVD-Metriken ausgeben.

        Returns:
            dict mit:
              - cumulative_buy_volume
              - cumulative_sell_volume
              - cumulative_delta        (buy - sell, total)
              - rolling_buy_volume      (Fenster)
              - rolling_sell_volume     (Fenster)
              - rolling_delta           (Fenster)
              - trade_count
              - last_price
              - last_timestamp
        """
        return {
            "cumulative_buy_volume":  round(self.cumulative_buy_volume, 8),
            "cumulative_sell_volume": round(self.cumulative_sell_volume, 8),
            "cumulative_delta":       round(self.cumulative_delta, 8),
            "rolling_buy_volume":     round(self._rolling_buy, 8),
            "rolling_sell_volume":    round(self._rolling_sell, 8),
            "rolling_delta":          round(self._rolling_delta, 8),
            "trade_count":            self.trade_count,
            "last_price":             self.last_price,
            "last_timestamp":         self.last_timestamp,
        }

    @property
    def rolling_delta(self) -> float:
        """Netto-Delta über das konfigurierte Rolling-Fenster."""
        return self._rolling_delta

    @property
    def cvd_ratio(self) -> Optional[float]:
        """Verhältnis (buy - sell) / (buy + sell) im Fenster.
        Rückgabe: float zwischen -1 und 1, oder None wenn kein Volumen.
        """
        total = self._rolling_buy + self._rolling_sell
        if total == 0:
            return None
        return round((self._rolling_buy - self._rolling_sell) / total, 6)
