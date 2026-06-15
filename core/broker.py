import asyncio
from typing import Dict, List


class Broker:
    """In-process message bus using asyncio.Queue — replaces Redis Pub/Sub."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[asyncio.Queue]] = {}

    def subscribe(self, channel: str, maxsize: int = 200) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.setdefault(channel, []).append(q)
        return q

    async def publish(self, channel: str, message: dict) -> None:
        for q in self._subscribers.get(channel, []):
            if q.full():
                try:
                    q.get_nowait()  # drop oldest if subscriber is slow
                except asyncio.QueueEmpty:
                    pass
            await q.put(message)


# Channels
L2 = "l2"
TRADES = "trades"
AGGREGATED = "aggregated"
SIGNALS = "signals"
