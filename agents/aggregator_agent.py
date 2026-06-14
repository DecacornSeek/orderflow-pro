"""
Aggregator Agent - CVD-Berechnung aus L2 + Trade Streams.

Subscribed auf:
  - binance_l2     (L2 Order Book Updates)
  - binance_trades (Trade Stream mit aggressor_side)

Publiziert auf:
  - aggregated_cvd (CVD-Metriken pro Exchange)

Architektur:
  - Eine asyncio Task subscribed auf beide Redis Pub/Sub Channels
  - CVD-Instanz pro Exchange pflegt Rolling Delta + Cumulative Delta
  - Alle N Sekunden (publish_interval) wird ein CVD-Snapshot auf
    aggregated_cvd gepusht
  - Bei jedem Trade wird live die CVD aktualisiert
  - L2 Updates werden zur Anreicherung genutzt (mid_price zum Zeitpunkt des Trades)

Laufzeit:
  - Endlos-Loop mit Auto-Reconnect (exponential backoff)
  - Graceful Shutdown via Signal
"""

import asyncio
import json
import logging
import signal
import time
from typing import Any, Dict, Optional

import redis.asyncio as redis

from core.cvd import CVD

logger = logging.getLogger(__name__)

SYMBOL = "BTCUSDT"
REDIS_CHANNEL_L2 = "binance_l2"
REDIS_CHANNEL_TRADES = "binance_trades"
REDIS_CHANNEL_CVD = "aggregated_cvd"
PUBLISH_INTERVAL = 1.0  # CVD-Snapshot alle 1s pushen
MAX_BACKOFF = 60.0


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )


class AggregatorAgent:
    """Aggregiert L2 + Trades, berechnet CVD und publiziert nach Redis."""

    def __init__(self, redis_url: str = "redis://localhost:6379") -> None:
        self.redis_url = redis_url
        self.redis_client: Optional[redis.Redis] = None
        self.pubsub: Optional[redis.client.PubSub] = None

        # CVD State - aktuell nur Binance, spaeter multi-exchange
        self.cvd: CVD = CVD(window_size=200)

        # Letzter L2 Snapshot (fuer Kontext / mid_price)
        self.last_l2: Dict[str, Any] = {}

        self.shutdown_event: asyncio.Event = asyncio.Event()
        self._tasks: list = []

    async def _connect(self) -> None:
        """Verbindung zu Redis herstellen (mit Backoff)."""
        backoff = 1.0
        while not self.shutdown_event.is_set():
            try:
                self.redis_client = redis.from_url(
                    self.redis_url, decode_responses=True
                )
                self.pubsub = self.redis_client.pubsub()
                await self.pubsub.subscribe(REDIS_CHANNEL_L2, REDIS_CHANNEL_TRADES)
                logger.info("Mit Redis verbunden und subscribed auf %s, %s",
                            REDIS_CHANNEL_L2, REDIS_CHANNEL_TRADES)
                return
            except Exception as e:
                logger.error("Redis Connect fehlgeschlagen: %s. Retry in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    async def _handle_l2(self, data: Dict[str, Any]) -> None:
        """L2 Update speichern (fuer Kontext / mid_price)."""
        self.last_l2 = data

    async def _handle_trade(self, data: Dict[str, Any]) -> None:
        """Trade verarbeiten -> CVD updaten."""
        price = float(data["price"])
        size = float(data["size"])
        side = data["aggressor_side"]
        ts = data.get("timestamp")

        self.cvd.update(price, size, side, timestamp=ts)

    async def _publish_loop(self) -> None:
        """Periodisch CVD-Snapshot auf aggregated_cvd pushen."""
        while not self.shutdown_event.is_set():
            await asyncio.sleep(PUBLISH_INTERVAL)

            if self.redis_client is None:
                continue

            try:
                cvd_snap = self.cvd.snapshot()
                payload = {
                    "exchange": "binance",
                    "symbol": SYMBOL,
                    "timestamp": int(time.time() * 1000),
                    "cvd": cvd_snap,
                    "mid_price": self.last_l2.get("mid_price"),
                }
                await self.redis_client.publish(
                    REDIS_CHANNEL_CVD, json.dumps(payload)
                )
                logger.debug("CVD published: rolling_delta=%.4f cum_delta=%.4f",
                             cvd_snap["rolling_delta"], cvd_snap["cumulative_delta"])
            except Exception as e:
                logger.error("Fehler beim CVD-Publish: %s", e)

    async def _subscribe_loop(self) -> None:
        """Redis Pub/Sub Nachrichten verarbeiten (L2 + Trades)."""
        backoff = 1.0

        while not self.shutdown_event.is_set():
            try:
                if self.pubsub is None:
                    await self._connect()

                async for msg in self.pubsub.listen():
                    if self.shutdown_event.is_set():
                        break
                    if msg["type"] != "message":
                        continue

                    channel = msg["channel"]
                    try:
                        data = json.loads(msg["data"])
                    except json.JSONDecodeError:
                        logger.warning("Ungueltiges JSON erhalten auf %s", channel)
                        continue

                    if channel == REDIS_CHANNEL_L2:
                        await self._handle_l2(data)
                    elif channel == REDIS_CHANNEL_TRADES:
                        await self._handle_trade(data)

                    backoff = 1.0

            except Exception as e:
                logger.error("Subscribe Loop Fehler: %s. Reconnect in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

        logger.info("Subscribe loop beendet.")

    async def run(self) -> None:
        """Aggregator Agent starten."""
        logger.info("Aggregator Agent gestartet.")

        await self._connect()

        subscribe_task = asyncio.create_task(self._subscribe_loop())
        publish_task = asyncio.create_task(self._publish_loop())
        self._tasks = [subscribe_task, publish_task]

        try:
            await self.shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Shutdown - schliesse Verbindungen...")
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            if self.pubsub:
                await self.pubsub.unsubscribe()
            if self.redis_client:
                await self.redis_client.aclose()
            logger.info("Aggregator Agent gestoppt.")


async def main():
    configure_logging()
    agent = AggregatorAgent()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: agent.shutdown_event.set())
        except NotImplementedError:
            pass

    await agent.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
