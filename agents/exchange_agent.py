import asyncio
import logging
import time

import ccxt.pro as ccxtpro

from core.broker import Broker, L2, TRADES
from core.orderbook import OrderBook
from core.validators import validate_trade_event, validate_l2_snapshot
import core.metrics as _metrics

logger = logging.getLogger(__name__)

SYMBOL = "BTC/USDT"
MAX_BACKOFF = 60.0


async def run(broker: Broker, shutdown: asyncio.Event) -> None:
    exchange = ccxtpro.binance({"enableRateLimit": False})
    book = OrderBook("BTCUSDT", depth=100)

    l2_task = asyncio.create_task(_l2_loop(exchange, broker, book, shutdown))
    trade_task = asyncio.create_task(_trade_loop(exchange, broker, shutdown))

    await asyncio.gather(l2_task, trade_task, return_exceptions=True)
    await exchange.close()
    logger.info("Exchange agent gestoppt.")


async def _l2_loop(exchange, broker: Broker, book: OrderBook, shutdown: asyncio.Event) -> None:
    backoff = 1.0
    logger.info("Binance L2 stream gestartet.")

    while not shutdown.is_set():
        try:
            ob = await exchange.watch_order_book(SYMBOL, limit=100)
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            nonce = ob.get("nonce") or int(time.time() * 1000)

            book.apply_snapshot(bids, asks, nonce)
            metrics = book.metrics()
            top = book.top(20)

            l2_event = {
                "exchange": "binance",
                "timestamp": ob.get("timestamp") or int(time.time() * 1000),
                "bids": top["bids"],
                "asks": top["asks"],
                **metrics,
            }
            ok, reason = validate_l2_snapshot(l2_event)
            if not ok:
                logger.warning("L2 snapshot ungültig — übersprungen: %s | keys=%s", reason, list(l2_event.keys()))
                _metrics.increment(_metrics.VALIDATION_L2_FAILED)
                _metrics.increment(_metrics.MESSAGES_SKIPPED)
            else:
                await broker.publish(L2, l2_event)
            backoff = 1.0

        except Exception as e:
            logger.warning(f"L2 Fehler: {e} — Retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)


async def _trade_loop(exchange, broker: Broker, shutdown: asyncio.Event) -> None:
    backoff = 1.0
    logger.info("Binance Trade stream gestartet.")

    while not shutdown.is_set():
        try:
            trades = await exchange.watch_trades(SYMBOL)
            for t in trades:
                info = t.get("info", {})
                side = "sell" if info.get("m", False) else "buy"
                event = {
                    "exchange": "binance",
                    "timestamp": t.get("timestamp") or int(time.time() * 1000),
                    "price": t.get("price"),
                    "size": t.get("amount"),
                    "side": side,
                }
                ok, reason = validate_trade_event(event)
                if not ok:
                    logger.warning("Trade event ungültig — übersprungen: %s | payload=%r", reason, event)
                    _metrics.increment(_metrics.VALIDATION_TRADE_FAILED)
                    _metrics.increment(_metrics.MESSAGES_SKIPPED)
                    continue
                await broker.publish(TRADES, event)
            backoff = 1.0

        except Exception as e:
            logger.warning(f"Trade Fehler: {e} — Retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
