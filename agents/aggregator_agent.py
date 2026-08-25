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
from core.pattern_engine import PatternEngine, detect_delta_divergence
from core.session_profile import SessionProfile
from core.weekly_profile import WeeklyProfile
from core.profile_shape import classify_shape
from core.absorption import AbsorptionDetector
from core.divergence import DivergenceDetector
from core.composite_profile import CompositeProfile
from core.profile_structure import structure_context
from core.business_zones import ZoneRegistry
from core.road_map import build_road_map
from core.lethargy import LethargyDetector
from core.vpoc_trend import classify_vpoc_trend

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
    zones = ZoneRegistry()
    lethargy = LethargyDetector()
    last_l2: dict = {}

    l2_q = broker.subscribe(L2)
    trade_q = broker.subscribe(TRADES)

    consume_l2 = asyncio.create_task(_consume_l2(l2_q, last_l2, shutdown))
    consume_trades = asyncio.create_task(
        _consume_trades(trade_q, last_l2, cvd, engine, session, weekly,
                        absorption, lethargy, broker, shutdown)
    )
    publish = asyncio.create_task(
        _publish_loop(broker, cvd, last_l2, session, weekly, absorption,
                      divergence, composite, zones, lethargy, shutdown)
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
    lethargy: LethargyDetector,
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
                # Lethargy: track price+volume to detect slowing at zones
                lethargy.ingest(price_f, size_f, ts)
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
    zones: ZoneRegistry,
    lethargy: LethargyDetector,
    shutdown: asyncio.Event,
) -> None:
    """Periodically publish aggregated snapshot on AGGREGATED with market structure context."""
    known_profiles = -1  # Zonen nur neu bauen, wenn neue Profile archiviert wurden
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
            weekly_archived = weekly.get_archived_profiles()
            if archived:
                composite.add_profiles(archived[-5:])  # last 5 sessions
            composite_ctx = composite.current_context()

            # Profil-Struktur der LAUFENDEN Session (Step 5: Single Prints,
            # schwache/starke Extreme, Double Distribution)
            try:
                structure_ctx = structure_context(
                    session._vap if session.current_session and session._vap else None
                )
            except Exception as e:
                _throttled_warn("structure_ctx", "profile structure failed: %s", e)
                structure_ctx = structure_context(None)

            # Business Zones (Step 6) + Road Map (Step 7)
            try:
                # Monotonic lifetime counts, not len(archived) — the archive
                # deques are ring buffers (session maxlen=60, weekly=52), so
                # once they saturate len() stops changing even though new
                # profiles keep being archived, which would freeze zone
                # rebuilds for days/weeks at a time in steady-state operation.
                total_profiles = session.archived_total_count + weekly.archived_total_count
                if total_profiles != known_profiles:
                    zones.rebuild(archived + weekly_archived)
                    known_profiles = total_profiles
                if mid_price is not None:
                    zones.update_price(float(mid_price))
                zones_ctx = zones.snapshot(float(mid_price) if mid_price is not None else None)
                road_map_ctx = build_road_map(session_ctx, weekly_ctx, zones_ctx,
                                              float(mid_price) if mid_price is not None else None)
            except Exception as e:
                _throttled_warn("zones_ctx", "business zones/road map failed: %s", e)
                metrics.increment(metrics.CONTEXT_FALLBACK)
                zones_ctx = {"zones": [], "zone_count": 0,
                             "n_unrepaired_single_prints": 0,
                             "zone_at": None, "zone_below": None, "zone_above": None}
                road_map_ctx = build_road_map(None, None, None, None)

            # Lethargy (Step 8): market slowing at nearest zone
            try:
                zone_below = zones_ctx.get("zone_below")
                zone_above = zones_ctx.get("zone_above")
                zone_at = zones_ctx.get("zone_at")
                # Use the nearest zone (at > below/above) for proximity check
                target_zone = zone_at or zone_below or zone_above
                if target_zone and mid_price is not None:
                    lethargy.set_zone(float(target_zone["price_low"]),
                                      float(target_zone["price_high"]))
                else:
                    lethargy.set_zone(None, None)
                lethargy_ctx = lethargy.assess()
            except Exception as e:
                _throttled_warn("lethargy_ctx", "lethargy detection failed: %s", e)
                lethargy_ctx = {"lethargy_detected": False, "lethargy_score": 0.0,
                                "message": f"error: {e}"}

            # Multi-Week VPOC Trend (Step 5): global money flow direction
            try:
                vpoc_trend_ctx = classify_vpoc_trend(weekly_archived)
            except Exception as e:
                _throttled_warn("vpoc_trend", "VPOC trend failed: %s", e)
                vpoc_trend_ctx = {"direction": "insufficient_data", "strength": 0.0,
                                  "message": f"error: {e}"}

            # Divergence
            div = divergence.current_divergence

            # Delta Divergenz aus bestätigten Swings (Spec §5.3).
            # cvd_spot_* bleibt None bis Sprint A die Spot-Streams liefert —
            # spot_confirms ist dann None statt True/False.
            try:
                highs = divergence.get_swing_highs()
                lows = divergence.get_swing_lows()
                delta_div = detect_delta_divergence(
                    price_highs=[s["price"] for s in highs],
                    cvd_perps_highs=[s["cvd"] for s in highs],
                    price_lows=[s["price"] for s in lows],
                    cvd_perps_lows=[s["cvd"] for s in lows],
                )
            except Exception as e:
                _throttled_warn("delta_div", "delta divergence failed: %s", e)
                delta_div = None

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
                "delta_divergence": delta_div,
                "profile_structure": structure_ctx,
                "business_zones": zones_ctx,
                "road_map": road_map_ctx,
                "lethargy": lethargy_ctx,
                "vpoc_trend": vpoc_trend_ctx,
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
