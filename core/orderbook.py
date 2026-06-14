"""core/orderbook.py - Production-ready order book with snapshot and update."""

from typing import Dict, Iterable, List


class OrderBook:
    """Maintains the top depth levels of an order book for a given trading symbol."""

    def __init__(self, symbol: str, depth: int = 20) -> None:
        """Initialize the order book.

        Args:
            symbol: Trading pair / instrument identifier.
            depth: Maximum number of price levels to keep on each side.
        """
        self.symbol = symbol
        self.depth = depth
        self._bids: List[List[float]] = []
        self._asks: List[List[float]] = []

    def update(
        self,
        bids_raw: Iterable[Iterable[float]],
        asks_raw: Iterable[Iterable[float]],
    ) -> None:
        """Replace the entire order book with new raw data.

        Bids are sorted descending by price (best bid first). Asks are sorted
        ascending by price (best ask first). Only the top *depth* levels are
        kept. Entries whose size is zero are silently discarded.

        Args:
            bids_raw: Iterable of (price, size) pairs for the bid side.
            asks_raw: Iterable of (price, size) pairs for the ask side.
        """
        # ---- bids ----
        bids: List[List[float]] = []
        for entry in bids_raw:
            price = float(entry[0])
            size = float(entry[1])
            if size == 0:
                continue
            bids.append([price, size])
        # best bid = highest price -> descending order
        bids.sort(key=lambda x: x[0], reverse=True)
        self._bids = bids[:self.depth]

        # ---- asks ----
        asks: List[List[float]] = []
        for entry in asks_raw:
            price = float(entry[0])
            size = float(entry[1])
            if size == 0:
                continue
            asks.append([price, size])
        # best ask = lowest price -> ascending order
        asks.sort(key=lambda x: x[0])
        self._asks = asks[:self.depth]

    def snapshot(self) -> Dict[str, List[List[float]]]:
        """Return the current order book state.

        Returns:
            Dictionary with keys ``"bids"`` and ``"asks"``. Each value is a
            list of ``[price, size]`` pairs, sorted according to the rules
            described in :meth:`update`.
        """
        return {"bids": self._bids, "asks": self._asks}
