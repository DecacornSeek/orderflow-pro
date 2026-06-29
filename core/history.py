from collections import deque
from typing import Dict, List

BUCKET = 25  # $25 VAP buckets


class History:
    """60-Minuten In-Memory Ring Buffer für L2/CVD Snapshots + Volume at Price."""

    def __init__(self, max_seconds: int = 3600) -> None:
        self._snapshots: deque = deque(maxlen=max_seconds)
        # _vap is session-scoped: accumulates from process start, never trimmed.
        # Bitcoin trades across ~5000 $25-buckets ($10k–$135k range) ≈ 200KB max.
        self._vap: Dict[int, float] = {}   # price_bucket -> cumulative volume
        self._klines: List[dict] = []       # von Binance REST beim Start

    # --- klines (historische Kerzen) -----------------------------------------

    def set_klines(self, klines: List[dict]) -> None:
        self._klines = klines

    def get_klines(self) -> List[dict]:
        return self._klines

    # --- snapshots (Aggregator Ticks) ----------------------------------------

    def add_snapshot(self, snapshot: dict) -> None:
        self._snapshots.append(snapshot)

    def get_snapshots(self, last_n: int = 60) -> List[dict]:
        snaps = list(self._snapshots)
        return snaps[-last_n:]

    # --- Volume at Price ------------------------------------------------------

    def add_trade(self, price: float, size: float) -> None:
        bucket = int(price / BUCKET) * BUCKET
        self._vap[bucket] = self._vap.get(bucket, 0.0) + size

    def get_depth_frames(self, last_n: int = 60) -> List[dict]:
        """Return the last *last_n* snapshot frames, each with bids/asks.

        Each frame contains:
          - timestamp:  int (unix ms)
          - bids:       list of [price, size]
          - asks:       list of [price, size]

        Reuses the existing `_snapshots` deque ? no extra data structure.

        Args:
            last_n: Number of most recent frames to return.

        Returns:
            List of depth frames, newest last, each with sorted bid/ask levels.
        """
        return list(self._snapshots)[-last_n:]

    def get_vap(self) -> Dict[int, float]:
        return dict(self._vap)
