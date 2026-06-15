import asyncio
import logging
import time

from core.broker import Broker, L2, TRADES, AGGREGATED
from core.cvd import CVD

logger = logging.getLogger(__name__)

PUBLISH_INTERVAL = 1.0  # snapshot alle 1s publizieren


async def run(broker: Broker, shutdown: asyncio.Event) -> None:
    cvd = CVD(window_size=200)
    last_l2: dict = {}

    l2_q = broker.subscribe(L2)
    trade_q = broker.subscribe(TRADES)

    consume_l2 = asyncio.create_task(_consume(l2_q, "l2", last_l2, cvd, shutdown))
    consume_trades = asyncio.create_task(_consume(trade_q, "trades", last_l2, cvd, shutdown))
    publish = asyncio.create_task(_publish_loop(broker, cvd, last_l2, shutdown))

    await asyncio.gather(consume_l2, consume_trades, publish, return_exceptions=True)
    logger.info("Aggregator gestoppt.")


async def _consume(queue: asyncio.Queue, kind: str, last_l2: dict, cvd: CVD, shutdown: asyncio.Event) -> None:
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
                    cvd.update(float(price), float(size), side)
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.warning(f"Aggregator consume error ({kind}): {e}")


async def _publish_loop(broker: Broker, cvd: CVD, last_l2: dict, shutdown: asyncio.Event) -> None:
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
