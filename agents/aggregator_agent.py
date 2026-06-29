"""Aggregator Agent - CVD calculation + Pattern Engine integration.

Subscribes to L2 and TRADES channels via Broker.
Every PUBLISH_INTERVAL seconds, publishes an aggregated snapshot on AGGREGATED.
Each trade is also evaluated by PatternEngine; hits are published on PATTERNS.
"""

import asyncio
import logging
import time

from core.broker import Broker, L2, TRADES, AGGREGATED, PATTERNS
from core.cvd import CVD
from core.pattern_engine import PatternEngine

logger = logging.getLogger(__name__)

PUBLISH_INTERVAL = 1.0  # aggregator snapshot every 1s


async def run(broker: Broker, shutdown: asyncio.Event) -> None:
    cvd = CVD(window_size=200)
    engine = PatternEngine()
    last_l2: dict = {}

    l2_q = broker.subscribe(L2)
    trade_q = broker.subscribe(TRADES)

    consume_l2 = asyncio.create_task(_consume(l2_q, "l2", last_l2, cvd, shutdown))
    consume_trades = asyncio.create_task(
        _consume(trade_q, "trades", last_l2, cvd, engine, broker, shutdown)
    )
    publish = asyncio.create_task(_publish_loop(broker, cvd, last_l2, shutdown))

    await asyncio.gather(consume_l2, consume_trades, publish, return_exceptions=True)
    logger.info("Aggregator stopped.")


async def _consume(
    queue: asyncio.Queue,
    kind: str,
    last_l2: dict,
    cvd: CVD,
    shutdown: asyncio.Event,
    engine: PatternEngine = None,
    broker: Broker = None,
) -> None:
    """Consume messages from a single channel.

    Args:
        queue: The asyncio.Queue to consume from.
        kind: "l2" or "trades" - determines how the message is processed.
        last_l2: Shared mutable dict for the latest L2 snapshot.
        cvd: CVD instance for trade processing.
        shutdown: asyncio.Event for graceful shutdown.
        engine: PatternEngine instance (only used for "trades").
        broker: Broker instance (only used for "trades", to publish patterns).
    """
    while not shutdown.is_set():
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=1.0)
            if kind == "l2":
                last_l2.update(msg)
            elif kind == "trades":
                price = msg.get("price")
                size = msg.get("size")
                side = msg.get("side")
                if price and size and side:
                    snap = cvd.update(float(price), float(size), side)

                    # Pattern evaluation on every trade
                    if engine is not None and broker is not None:
                        result = engine.evaluate(msg, recent_cvd_snapshot=snap)
                        if result is not None:
                            await broker.publish(PATTERNS, result)
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.warning(f"Aggregator consume error ({kind}): {e}")


async def _publish_loop(
    broker: Broker, cvd: CVD, last_l2: dict, shutdown: asyncio.Event
) -> None:
    """Periodically publish aggregated snapshot on AGGREGATED."""
    while not shutdown.is_set():
        await asyncio.sleep(PUBLISH_INTERVAL)
        try:
            snap = cvd.snapshot()
            await broker.publish(AGGREGATED, {
                "timestamp": int(time.time() * 1000),
                "mid_price": last_l2.get("mid_price"),
                "best_bid": last_l2.get("best_bid"),
                "best_ask": last_l2.get("best_ask"),
                "spread": last_l2.get("spread"),
                "imbalance_5": last_l2.get("imbalance_5"),
                "imbalance_20": last_l2.get("imbalance_20"),
                "bids": last_l2.get("bids", []),
                "asks": last_l2.get("asks", []),
                "cvd": snap,
            })
        except Exception as e:
            logger.warning(f"Aggregator publish error: {e}")
