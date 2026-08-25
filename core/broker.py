"""
core/broker.py — In-Process Asyncio Message Bus fuer OrderFlow Pro.

Ermoeglicht lockere Kopplung zwischen den Agenten (Exchange, Aggregator,
Options, Display) ohne direkte Abhaengigkeiten.
In Produktion auf Hetzner VPS durch Redis Pub/Sub ersetzbar.

Zwei Subscriber-Formen, bewusst beide unterstuetzt:

  q = broker.subscribe(TRADES)              -> asyncio.Queue (Pull, Agent-Loops)
  broker.subscribe(TRADES, on_trade)        -> Callback (Push, Options-Agent)

Die Queue-Form ist die aeltere; Agent-Loops konsumieren mit `await q.get()`.
Die Callback-Form kam mit dem Options-Agent dazu. Beide teilen sich publish().
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# Standard Channel-Konstanten
TRADES = "trades"
L2 = "l2"
FUNDING = "funding"
LIQUIDATIONS = "liquidations"
AGGREGATED = "aggregated"
PATTERNS = "patterns"
SIGNALS = "signals"
CONTEXT = "context"
OPTIONS = "options"
OPTIONS_SNAPSHOT = "options_snapshot"

# Alt-Schreibweise aus einer frueheren Fassung — bleibt als Alias bestehen,
# damit bestehende Importe nicht brechen.
LIQUIDations = LIQUIDATIONS

DEFAULT_QUEUE_MAXSIZE = 200

Callback = Callable[[Any], Union[Coroutine[Any, Any, None], None]]


class Broker:
    """
    Einfacher in-memory Message Broker basierend auf asyncio.Queue und
    Callbacks fuer das Publisher/Subscriber-Entwurfsmuster.
    """

    def __init__(self) -> None:
        self._callbacks: Dict[str, List[Callback]] = {}
        self._queues: Dict[str, List[asyncio.Queue]] = {}

    def subscribe(
        self,
        channel: str,
        callback: Optional[Callback] = None,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ) -> Optional[asyncio.Queue]:
        """
        Ohne `callback`: legt eine neue Queue an, registriert und gibt sie zurueck.
        Mit `callback`: registriert die Funktion und gibt None zurueck.
        """
        if callback is None:
            queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
            self._queues.setdefault(channel, []).append(queue)
            return queue

        self._callbacks.setdefault(channel, []).append(callback)
        return None

    def subscribe_queue(self, channel: str, queue: asyncio.Queue) -> None:
        """Registriert eine bereits bestehende Queue als Subscriber."""
        self._queues.setdefault(channel, []).append(queue)

    def unsubscribe(self, channel: str, subscriber: Union[asyncio.Queue, Callback]) -> None:
        """Entfernt eine Queue oder einen Callback wieder."""
        if isinstance(subscriber, asyncio.Queue):
            try:
                self._queues.get(channel, []).remove(subscriber)
            except ValueError:
                pass
            return

        try:
            self._callbacks.get(channel, []).remove(subscriber)
        except ValueError:
            pass

    async def publish(self, channel: str, message: Any) -> None:
        """
        Publiziert eine Nachricht an alle Abonnenten des Channels.

        Fehler eines einzelnen Subscribers duerfen den Publisher nicht
        mitreissen — sie werden geloggt, nicht verschluckt (CLAUDE.md:
        "Fehler loggen, nie crashen").
        """
        for cb in list(self._callbacks.get(channel, [])):
            try:
                result = cb(message)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                logger.exception("Subscriber-Callback auf Channel '%s' fehlgeschlagen", channel)

        for queue in list(self._queues.get(channel, [])):
            # Bei vollem Puffer die aelteste Nachricht verwerfen, nicht die neueste:
            # ein langsamer Subscriber soll den aktuellen Marktzustand sehen.
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("Queue auf Channel '%s' weiterhin voll, Nachricht verworfen", channel)

    def subscriber_count(self, channel: str) -> int:
        """Anzahl registrierter Subscriber (Queues + Callbacks) auf einem Channel."""
        return len(self._queues.get(channel, [])) + len(self._callbacks.get(channel, []))
