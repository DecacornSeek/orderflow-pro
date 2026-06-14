import asyncio
import json
import logging
import signal
import time

import ccxt.pro as ccxtpro
import redis.asyncio as redis

# NOTE: This agent depends on the core.orderbook module providing OrderBook.
# Ensure the OrderBook class is importable from core.orderbook.
from core.orderbook import OrderBook

logger = logging.getLogger(__name__)

SYMBOL = 'BTC/USDT'
REDIS_CHANNEL_L2 = 'binance_l2'
REDIS_CHANNEL_TRADES = 'binance_trades'
MAX_BACKOFF = 60.0


def configure_logging():
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )


async def handle_order_book_stream(exchange: ccxtpro.binance, redis_client: redis.Redis,
                                   shutdown_event: asyncio.Event):
    """L2 order book stream worker with exponential backoff."""
    logger.info("Verbinde mit Binance WebSocket (L2 order book)...")
    backoff = 1.0
    orderbook = OrderBook("BTCUSDT", depth=100)
    first_update = True

    while not shutdown_event.is_set():
        try:
            ob = await exchange.watch_order_book(SYMBOL, limit=100)
            # Successful receive – reset backoff after processing
            bids = ob.get('bids')
            asks = ob.get('asks')
            last_update_id = ob.get('nonce')
            timestamp_ms = ob.get('timestamp')

            if not bids or not asks or last_update_id is None:
                logger.warning("Ungültiges Orderbuch-Update erhalten, überspringe")
                # Still consider the call successful → reset backoff
                backoff = 1.0
                continue

            # Sequence number validation
            if not first_update and last_update_id <= orderbook.last_update_id:
                logger.warning(f"Sequence Gap erkannt ({orderbook.last_update_id} vs {last_update_id}) — vollständiger Resync")

            # Apply the full snapshot – resets internal state
            orderbook.apply_snapshot(bids, asks, last_update_id)

            # Metrics + serialisable snapshot for Redis payload
            metrics = orderbook.metrics()
            snap = orderbook.snapshot()

            message = {
                "exchange": "binance",
                "symbol": "BTCUSDT",
                "timestamp": timestamp_ms if timestamp_ms else int(time.time() * 1000),
                "bids": snap["bids"],
                "asks": snap["asks"],
                "imbalance_5": metrics["imbalance_5"],
                "imbalance_20": metrics["imbalance_20"],
                "spread": metrics["spread"],
                "mid_price": metrics["mid_price"],
                "last_update_id": last_update_id
            }

            await redis_client.publish(REDIS_CHANNEL_L2, json.dumps(message))
            logger.debug(
                "L2 Update: lastUpdateId=%s, spread=%s",
                last_update_id,
                f"{metrics['spread']:.2f}" if metrics["spread"] is not None else "n/a"
            )

            first_update = False
            backoff = 1.0   # reset after successful message

        except Exception as e:
            logger.error(f"Verbindung unterbrochen (L2): {e}. Reconnect in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    logger.info("L2 order book stream beendet.")


async def handle_trade_stream(exchange: ccxtpro.binance, redis_client: redis.Redis,
                              shutdown_event: asyncio.Event):
    """Trade stream worker with exponential backoff."""
    logger.info("Verbinde mit Binance WebSocket (Trades)...")
    backoff = 1.0

    while not shutdown_event.is_set():
        try:
            trades = await exchange.watch_trades(SYMBOL)
            if trades:
                for trade in trades:
                    info = trade.get('info', {})
                    # Binance field 'm': isBuyerMaker.
                    # m == True → seller is aggressor → "sell"
                    # m == False → buyer is aggressor → "buy"
                    maker = info.get('m', False)
                    aggressor_side = "sell" if maker else "buy"

                    msg = {
                        "exchange": "binance",
                        "symbol": "BTCUSDT",
                        "timestamp": trade.get('timestamp'),
                        "price": trade.get('price'),
                        "size": trade.get('amount'),
                        "aggressor_side": aggressor_side,
                        "trade_id": str(trade.get('id'))
                    }
                    await redis_client.publish(REDIS_CHANNEL_TRADES, json.dumps(msg))
                    logger.debug(f"Trade: {aggressor_side} {trade.get('amount')} @ {trade.get('price')}")
            backoff = 1.0   # reset after successful call

        except Exception as e:
            logger.error(f"Verbindung unterbrochen (Trades): {e}. Reconnect in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    logger.info("Trade stream beendet.")


async def main():
    configure_logging()
    shutdown_event = asyncio.Event()

    # Register signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: shutdown_event.set())
        except NotImplementedError:
            pass  # Windows compatibility

    redis_client = redis.Redis(decode_responses=False)

    exchange = ccxtpro.binance({
        'enableRateLimit': False,
        'options': {
            'newUpdates': True   # Ensures watch_trades only returns new trades
        }
    })

    l2_task = asyncio.create_task(handle_order_book_stream(exchange, redis_client, shutdown_event))
    trade_task = asyncio.create_task(handle_trade_stream(exchange, redis_client, shutdown_event))

    tasks = [l2_task, trade_task]

    try:
        await shutdown_event.wait()
        logger.info("Shutdown signal empfangen. Beende alle Streams.")
    except asyncio.CancelledError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        await exchange.close()
        await redis_client.aclose()
        logger.info("Alles sauber beendet.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
