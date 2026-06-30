"""Aggregator Agent - CVD calculation + Pattern Engine + Market Structure Context Layer.

Subscribes to L2 and TRADES channels via Broker.
Every PUBLISH_INTERVAL seconds, publishes an aggregated snapshot on AGGREGATED
with session context, weekly context, profile shape, absorption, divergence,
and composite context.
"""

import asyncio
import logging
import time

from core.broker import Broker, L2, TRADES, AGGREGATED, PATTERNS
from core.validators import validate_aggregated_snapshot
import core.metrics as metrics
from core.cvd import CVD
from core.pattern_engine import PatternEngine
from core.session_profile import SessionProfile
from core.weekly_profile import WeeklyProfile
from core.profile_shape import classify_shape
from core.absorption import AbsorptionDetector
from core.divergence import DivergenceDetector
from core.composite_profile import CompositeProfile

logger = logging.getLogger(__name__)

PUBLISH_INTERVAL = 1.0

# Throttle repeated context-failure warnings: one log per key per 60 s.
# Counter is still incremented on every failure — the counter is the source of truth.
_warned_at: dict[str, float] = {}
_WARN_COOLDOWN = 60.0


def _throttled_warn(key: str, msg: str, *args: object) -> None:
    now = time.monotonic()
    if now - _warned_at.get(key, 0.0) >= _WARN_COOLDOWN:
        logger.warning(msg, *args)
        _warned_at[key] = now


async def run(broker: Broker, shutdown: asyncio.Event) -> None:
    cvd = CVD(window_size=200)
    engine = PatternEngine()
    session = SessionProfile()
    weekly = WeeklyProfile()
    absorption = AbsorptionDetector()
    divergence = DivergenceDetector(lookback=3)
    composite = CompositeProfile()
    last_l2: dict = {}

    l2_q = broker.subscribe(L2)
    trade_q = broker.subscribe(TRADES)

    consume_l2 = asyncio.create_task(_consume_l2(l2_q, last_l2, shutdown))
    consume_trades = asyncio.create_task(
        _consume_trades(trade_q, last_l2, cvd, engine, session, weekly,
                        absorption, broker, shutdown)
    )
    publish = asyncio.create_task(
        _publish_loop(broker, cvd, last_l2, session, weekly, absorption,
                      divergence, composite, shutdown)
    )

    await asyncio.gather(consume_l2, consume_trades, publish, return_exceptions=True)
    logger.info("Aggregator stopped.")


async def _consume_l2(
    queue: asyncio.Queue,
    last_l2: dict,
    shutdown: asyncio.Event,
) -> None:
    while not shutdown.is_set():
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=1.0)
            last_l2.update(msg)
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.warning("Aggregator L2 consume error: %s", e)


async def _consume_trades(
    queue: asyncio.Queue,
    last_l2: dict,
    cvd: CVD,
    engine: PatternEngine,
    session: SessionProfile,
    weekly: WeeklyProfile,
    absorption: AbsorptionDetector,
    broker: Broker,
    shutdown: asyncio.Event,
) -> None:
    while not shutdown.is_set():
        try:
            msg = await asyncio.wait_for(queue.get(), timeout=1.0)
            price = msg.get("price")
            size = msg.get("size")
            side = msg.get("side")
            ts = msg.get("timestamp", int(time.time() * 1000))
            if price and size and side:
                price_f = float(price)
                size_f = float(size)

                # Core CVD + Pattern Engine
                snap = cvd.update(price_f, size_f, side)
                result = engine.evaluate(msg, recent_cvd_snapshot=snap)
                if result is not None:
                    await broker.publish(PATTERNS, result)

                # Context layer ingestion
                session.ingest(ts, price_f, size_f, side)
                weekly.ingest(ts, price_f, size_f, side)
                event = absorption.ingest(
                    {"price": price_f, "size": size_f, "side": side, "timestamp": ts},
                    cvd_snapshot=snap,
                    current_price=last_l2.get("mid_price"),
                )
                if event:
                    # Absorption events are included in the next aggregated snapshot
                    # via session context. They are not published separately.
                    pass
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.warning("Aggregator trades consume error: %s", e)


async def _publish_loop(
    broker: Broker,
    cvd: CVD,
    last_l2: dict,
    session: SessionProfile,
    weekly: WeeklyProfile,
    absorption: AbsorptionDetector,
    divergence: DivergenceDetector,
    composite: CompositeProfile,
    shutdown: asyncio.Event,
) -> None:
    """Periodically publish aggregated snapshot on AGGREGATED with market structure context."""
    while not shutdown.is_set():
        await asyncio.sleep(PUBLISH_INTERVAL)
        try:
            snap = cvd.snapshot()
            mid_price = last_l2.get("mid_price")

            # Divergence: feed current CVD delta + price
            if mid_price is not None:
                divergence.ingest(
                    int(time.time() * 1000),
                    float(mid_price),
                    snap.get("rolling_delta", 0.0),
                )

            # Session context — isolated so a module failure doesn't suppress the publish
            try:
                session_ctx = session.current_context()
                session_name = session_ctx.get("session", "")
                anomaly = session.get_pre_session_anomaly(session_name)
                if anomaly:
                    session_ctx["pre_session_anomaly"] = anomaly
                shape_ctx = classify_shape(session._vap if session.current_session and session._vap else None)
            except Exception as e:
                _throttled_warn("session_ctx", "session context failed: %s", e)
                metrics.increment(metrics.CONTEXT_SESSION_FAIL)
                metrics.increment(metrics.CONTEXT_FALLBACK)
                session_ctx = {"session": "N/A"}
                shape_ctx = classify_shape(None)

            # Weekly context
            try:
                weekly_ctx = weekly.current_context()
            except Exception as e:
                _throttled_warn("weekly_ctx", "weekly context failed: %s", e)
                metrics.increment(metrics.CONTEXT_WEEKLY_FAIL)
                metrics.increment(metrics.CONTEXT_FALLBACK)
                weekly_ctx = {"week": "N/A"}

            # Composite context from archived profiles
            archived = session.get_archived_profiles()
            if archived:
                composite.add_profiles(archived[-5:])  # last 5 sessions
            composite_ctx = composite.current_context()

            # Divergence
            div = divergence.current_divergence

            # Record window volume for absorption baseline
            total_vol = sum(t[2] for t in list(absorption._trades) if hasattr(absorption, "_trades"))
            absorption.record_window_volume(total_vol)

            payload = {
                "timestamp": int(time.time() * 1000),
                "mid_price": mid_price,
                "best_bid": last_l2.get("best_bid"),
                "best_ask": last_l2.get("best_ask"),
                "spread": last_l2.get("spread"),
                "imbalance_5": last_l2.get("imbalance_5"),
                "imbalance_20": last_l2.get("imbalance_20"),
                "bids": last_l2.get("bids", []),
                "asks": last_l2.get("asks", []),
                "cvd": snap,
                # Context layer
                "session_context": session_ctx,
                "profile_shape": shape_ctx,
                "weekly_context": weekly_ctx,
                "composite_context": composite_ctx,
                "divergence": div,
            }
            ok, reason = validate_aggregated_snapshot(payload)
            if not ok:
                logger.warning("Aggregated snapshot ungültig — übersprungen: %s | keys=%s", reason, list(payload.keys()))
                metrics.increment(metrics.VALIDATION_AGGREGATED_FAILED)
                metrics.increment(metrics.MESSAGES_SKIPPED)
                continue
            await broker.publish(AGGREGATED, payload)
        except Exception as e:
            logger.warning(f"Aggregator publish error: {e}")
