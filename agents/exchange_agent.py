import asyncio
import json
import logging
import signal
import time
from typing import Any, Dict, List, Optional

import ccxt.pro as ccxtpro
import redis.asyncio

from core.orderbook import OrderBook

logger = logging.getLogger(__name__)


class ExchangeAgent:
    """Connects to Binance via ccxt.pro, maintains a live L2 order book top 20
    levels using OrderBook, extracts aggressor side from trades, and publishes
    every update as JSON to a Redis channel."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        redis_url: str = "redis://localhost:6379",
        exchange_id: str = "binance",
    ) -> None:
        self.symbol = symbol
        self.redis_url = redis_url
        self.exchange_id = exchange_id
        self.orderbook = OrderBook(symbol, depth=20)
        self.last_trade: Optional[Dict[str, Any]] = None
        self.shutdown_event = asyncio.Event()
        self.redis: Optional[redis.asyncio.Redis] = None
        self._ob_task: Optional[asyncio.Task] = None
        self._trade_task: Optional[asyncio.Task] = None

    async def setup_redis(self) -> None:
        """Create the Redis connection pool."""
        self.redis = redis.asyncio.from_url(
            self.redis_url, decode_responses=True
        )

    async def sleep_or_shutdown(self, delay: float) -> None:
        """Sleep for `delay` seconds but return early if shutdown is requested."""
        try:
            await asyncio.wait_for(asyncio.shield(self.shutdown_event.wait()), timeout=delay)
        except asyncio.TimeoutError:
            pass  # delay expired normally

    async def order_book_loop(self, exchange: ccxtpro.Exchange) -> None:
        """Infinite loop to watch order book and publish updates."""
        while not self.shutdown_event.is_set():
            try:
                orderbook_data = await exchange.watch_order_book(self.symbol, limit=20)
                # Provide the full snapshot to OrderBook
                self.orderbook.update(
                    bids=orderbook_data["bids"],
                    asks=orderbook_data["asks"],
                )
                snap = self.orderbook.snapshot()
                await self.publish_update(snap["bids"], snap["asks"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in order book loop")
                raise  # let outer logic reconnect

    async def trade_loop(self, exchange: ccxtpro.Exchange) -> None:
        """Infinite loop to watch trades and publish updates."""
        while not self.shutdown_event.is_set():
            try:
                trades = await exchange.watch_trades(self.symbol)
                if trades:
                    last = trades[-1]
                    side = last.get("side", "buy")
                    # Binance public stream doesn't set takerOrMaker directly;
                    # derive from info['m'] (isBuyerMaker):
                    #   m=True  -> buyer is maker -> seller is aggressor (taker)
                    #   m=False -> buyer is taker (aggressor)
                    is_buyer_maker = last.get("info", {}).get("m", None)
                    if is_buyer_maker is not None:
                        aggressor = "taker" if (side == "sell" and is_buyer_maker) or (side == "buy" and not is_buyer_maker) else "maker"
                    else:
                        aggressor = last.get("takerOrMaker") or "taker"
                    price = last.get("price", 0)
                    size = last.get("amount", 0)
                    self.last_trade = {
                        "price": price,
                        "size": size,
                        "side": side,
                        "aggressor": aggressor,
                    }
                    # Publish the current order book together with the latest trade
                    snap = self.orderbook.snapshot()
                    await self.publish_update(snap["bids"], snap["asks"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in trade loop")
                raise

    async def publish_update(
        self, bids: List[List[float]], asks: List[List[float]]
    ) -> None:
        """Publish order book and last trade snapshot to Redis."""
        if self.redis is None or self.shutdown_event.is_set():
            return

        # Fallback last_trade to satisfy the JSON schema until the first trade arrives
        lt = self.last_trade
        if lt is None:
            lt = {
                "price": 0,
                "size": 0,
                "side": "buy",
                "aggressor": "maker",
            }

        ts = int(time.time() * 1000)
        payload = {
            "ts": ts,
            "symbol": self.symbol,
            "bids": bids,
            "asks": asks,
            "last_trade": lt,
        }

        try:
            await self.redis.publish("binance_orderbook", json.dumps(payload))
        except Exception:
            logger.exception("Failed to publish to Redis")

    async def shutdown(self, sig: Optional[int] = None) -> None:
        """Trigger graceful shutdown."""
        if self.shutdown_event.is_set():
            return
        logger.info(f"Shutdown signal received (signal={sig})")
        self.shutdown_event.set()
        # Cancel running watch tasks so that the asyncio.wait() unblocks
        if self._ob_task:
            self._ob_task.cancel()
        if self._trade_task:
            self._trade_task.cancel()

    async def cleanup(self) -> None:
        """Close Redis connection and finalize."""
        if self.redis:
            try:
                await self.redis.close()
            except Exception:
                logger.exception("Error closing Redis")
        logger.info("Shutdown complete.")

    async def run(self) -> None:
        """Main entry point – handle connections, reconnection and graceful shutdown."""
        await self.setup_redis()

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(self.shutdown(s)),
                )
            except NotImplementedError:
                # Windows does not support add_signal_handler for SIGTERM etc.
                pass

        backoff = 1.0
        max_backoff = 60.0

        while not self.shutdown_event.is_set():
            try:
                exchange = ccxtpro.binance({"enableRateLimit": True})
                logger.info(f"Connecting to {self.exchange_id} for {self.symbol}")
                await exchange.load_markets()
                logger.info("Connected. Starting watch tasks.")

                self._ob_task = asyncio.create_task(self.order_book_loop(exchange))
                self._trade_task = asyncio.create_task(self.trade_loop(exchange))

                done, pending = await asyncio.wait(
                    [self._ob_task, self._trade_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # If shutdown was requested, cancel the other task and exit loop
                if self.shutdown_event.is_set():
                    for task in pending:
                        task.cancel()
                    break

                # One of the tasks finished with an error – cancel the remaining one
                for task in pending:
                    task.cancel()

                # Wait for cancellations and log any exceptions
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        logger.error("Task failed with error", exc_info=exc)

                await exchange.close()

                if self.shutdown_event.is_set():
                    break

                # Reconnect with exponential backoff
                delay = min(backoff, max_backoff)
                logger.info(f"Reconnecting in {delay:.1f} seconds...")
                await self.sleep_or_shutdown(delay)
                if self.shutdown_event.is_set():
                    break
                backoff = min(backoff * 2, max_backoff)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Connection loop error")
                if self.shutdown_event.is_set():
                    break
                delay = min(backoff, max_backoff)
                logger.info(f"Reconnecting in {delay:.1f} seconds...")
                await self.sleep_or_shutdown(delay)
                if self.shutdown_event.is_set():
                    break
                backoff = min(backoff * 2, max_backoff)

        await self.cleanup()


async def main() -> None:
    """Application entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    agent = ExchangeAgent(symbol="BTC/USDT")
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
