"""
core/broker.py — In-Process Asyncio Message Bus fuer OrderFlow Pro.

Ermoeglicht lockere Kopplung zwischen den Agenten (Exchange, Aggregator,
Options, Signal, Display) ohne direkte Abhaengigkeiten.
In Produktion auf Hetzner VPS durch Redis Pub/Sub ersetzbar.
"""

import asyncio
from typing import Any, Callable, Coroutine, Dict, List, Set

# Standard Channel-Konstanten
TRADES = "trades"
L2 = "l2"
FUNDING = "funding"
LIQUIDations = "liquidations"
AGGREGATED = "aggregated"
PATTERNS = "patterns"
SIGNALS = "signals"
CONTEXT = "context"
OPTIONS = "options"
OPTIONS_SNAPSHOT = "options_snapshot"


class Broker:
    """
    Einfacher in-memory Message Broker basierend auf asyncio.Queue
    und Callbacks fuer Publisher/Subscriber-Entwurfsmuster.
    """

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Callable[[Any], Coroutine[Any, Any, None] | None]]] = {}
        self._queues: Dict[str, List[asyncio.Queue]] = {}

    def subscribe(self, channel: str, callback: Callable[[Any], Coroutine[Any, Any, None] | None]) -> None:
        """Registriert eine Callback-Funktion fuer einen Channel."""
        if channel not in self._subscribers:
            self._subscribers[channel] = []
        self._subscribers[channel].append(callback)

    def unsubscribe(self, channel: str, callback: Callable[[Any], Coroutine[Any, Any, None] | None]) -> None:
        """Entfernt eine registrierte Callback-Funktion."""
        if channel in self._subscribers:
            self._subscribers[channel] = [cb for cb in self._subscribers[channel] if cb != callback]

    def subscribe_queue(self, channel: str, queue: asyncio.Queue) -> None:
        """Fuegt eine asyncio.Queue als Subscriber hinzu."""
        if channel not in self._queues:
            self._queues[channel] = []
        self._queues[channel].append(queue)

    async def publish(self, channel: str, message: Any) -> None:
        """
        Publiziert eine Nachricht auf einem Channel an alle Abonnenten.
        Fuehrt Callbacks asynchron aus ohne blockierende Fehler.
        """
        # Callbacks benachrichtigen
        if channel in self._subscribers:
            for cb in self._subscribers[channel]:
                try:
                    res = cb(message)
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(res)
                except Exception as exc:
                    pass

        # Queues befuellen
        if channel in self._queues:
            for q in self._queues[channel]:
                try:
                    q.put_nowait(message)
                except asyncio.QueueFull:
                    pass
