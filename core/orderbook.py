"""Production-ready order book for a trading system.

Maintains top-of-book price levels for bids and asks with snapshot and delta
update semantics.  Sequence-number validation prevents applying updates out
of order.
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple, Union


class OrderBook:
    """High-performance order book tracking top *depth* levels per side."""

    def __init__(self, symbol: str, depth: int = 100) -> None:
        """Initialise an empty order book.

        Args:
            symbol: Trading pair identifier (e.g. 'BTCUSDT').
            depth: Maximum number of price levels to retain on each side.
        """
        self.symbol: str = symbol
        self.depth: int = depth
        self.bids: Dict[float, float] = {}  # price -> size (best bids are highest)
        self.asks: Dict[float, float] = {}  # price -> size (best asks are lowest)
        self.last_update_id: int = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_entries(
        raw: Union[Dict[float, float], Iterable[Tuple[float, float]]],
    ) -> List[Tuple[float, float]]:
        """Normalise raw price-size input into a list of (price, size) tuples.

        Accepts either a dict or any iterable of [price, size] pairs.
        """
        if isinstance(raw, dict):
            return list(raw.items())
        return [(float(price), float(size)) for price, size in raw]

    def _trim_bids(self) -> None:
        """Keep only the highest *depth* bid prices."""
        if len(self.bids) <= self.depth:
            return
        top = sorted(self.bids.keys(), reverse=True)[: self.depth]
        self.bids = {p: self.bids[p] for p in top}

    def _trim_asks(self) -> None:
        """Keep only the lowest *depth* ask prices."""
        if len(self.asks) <= self.depth:
            return
        top = sorted(self.asks.keys())[: self.depth]
        self.asks = {p: self.asks[p] for p in top}

    def _top_bids(self, n: int) -> List[Tuple[float, float]]:
        """Return top *n* bids sorted descending by price."""
        prices = sorted(self.bids.keys(), reverse=True)[:n]
        return [(p, self.bids[p]) for p in prices]

    def _top_asks(self, n: int) -> List[Tuple[float, float]]:
        """Return top *n* asks sorted ascending by price."""
        prices = sorted(self.asks.keys())[:n]
        return [(p, self.asks[p]) for p in prices]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply_snapshot(
        self,
        bids_raw: Union[Dict[float, float], Iterable[Tuple[float, float]]],
        asks_raw: Union[Dict[float, float], Iterable[Tuple[float, float]]],
        last_update_id: int,
    ) -> None:
        """Replace the entire order book with a snapshot.

        Zero-size entries are discarded.  The book is trimmed to *depth*
        levels per side after loading.

        Args:
            bids_raw: Bid price levels.
            asks_raw: Ask price levels.
            last_update_id: Sequence identifier of the snapshot.
        """
        # Rebuild bids, dropping zero sizes
        self.bids.clear()
        for price, size in self._parse_entries(bids_raw):
            if size > 0:
                self.bids[price] = size

        # Rebuild asks, dropping zero sizes
        self.asks.clear()
        for price, size in self._parse_entries(asks_raw):
            if size > 0:
                self.asks[price] = size

        self._trim_bids()
        self._trim_asks()
        self.last_update_id = last_update_id

    def apply_delta(
        self,
        bids_delta: Union[Dict[float, float], Iterable[Tuple[float, float]]],
        asks_delta: Union[Dict[float, float], Iterable[Tuple[float, float]]],
        update_id: int,
    ) -> bool:
        """Apply an incremental update.

        Returns:
            True on success, False when a sequence gap is detected
            (*update_id* != *last_update_id* + 1).  The caller must
            re-synchronise via :meth:`apply_snapshot` on False.

        Args:
            bids_delta: Bid changes.  size == 0 deletes the level;
                size > 0 inserts / updates it.
            asks_delta: Ask changes.  Same semantics as bids.
            update_id: Monotonic sequence number of this delta.
        """
        if update_id != self.last_update_id + 1:
            return False

        for price, size in self._parse_entries(bids_delta):
            if size <= 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = size

        for price, size in self._parse_entries(asks_delta):
            if size <= 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = size

        self._trim_bids()
        self._trim_asks()
        self.last_update_id = update_id
        return True

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serialisable view of the current order book.

        Returns:
            dict with keys ``bids`` (descending price), ``asks`` (ascending
            price), and ``last_update_id``.
        """
        return {
            "bids": [[p, s] for p, s in self._top_bids(self.depth)],
            "asks": [[p, s] for p, s in self._top_asks(self.depth)],
            "last_update_id": self.last_update_id,
        }

    def metrics(self) -> Dict[str, Optional[float]]:
        """Compute real-time market metrics from the book.

        Returns:
            dict with ``spread``, ``mid_price``, ``imbalance_5``, and
            ``imbalance_20``.  Values are ``None`` when the book does not
            contain sufficient data.
        """
        best_bid = max(self.bids.keys()) if self.bids else None
        best_ask = min(self.asks.keys()) if self.asks else None

        spread: Optional[float] = None
        mid_price: Optional[float] = None
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid
            mid_price = (best_bid + best_ask) / 2.0

        def _imbalance(n: int) -> Optional[float]:
            """Bid-ask volume imbalance over the top *n* levels."""
            bid_vol = sum(s for _, s in self._top_bids(n))
            ask_vol = sum(s for _, s in self._top_asks(n))
            total = bid_vol + ask_vol
            if total == 0:
                return None
            return (bid_vol - ask_vol) / total

        return {
            "spread": spread,
            "mid_price": mid_price,
            "imbalance_5": _imbalance(5),
            "imbalance_20": _imbalance(20),
        }
