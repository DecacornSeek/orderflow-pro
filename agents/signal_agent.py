"""Signal Agent — DeepSeek-powered market analysis with pattern + market structure context.

Subscribes to AGGREGATED snapshots (now enriched with session context, weekly
context, profile shape, absorption, divergence, composite context) and PATTERNS
events from the Broker.
"""

import asyncio
import logging
import os
import time
from collections import deque
from typing import Optional, Dict, Any

from openai import OpenAI

from core.broker import Broker, AGGREGATED, SIGNALS, PATTERNS

logger = logging.getLogger(__name__)

SIGNAL_INTERVAL = 15.0
HISTORY_SIZE = 10
PATTERN_CONTEXT_SIZE = 5
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"


def _load_api_key() -> Optional[str]:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("DEEPSEEK_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return None


def _format_session(session_ctx: Dict[str, Any]) -> str:
    """Format session context block for the prompt."""
    if not session_ctx or session_ctx.get("session") in (None, "N/A"):
        return ""
    parts = [f"  - Session: {session_ctx['session']}"]
    poc = session_ctx.get("session_poc")
    if poc is not None:
        parts.append(f"  - POC: ${poc}")
    va_h = session_ctx.get("session_value_area_high")
    va_l = session_ctx.get("session_value_area_low")
    if va_h is not None and va_l is not None:
        parts.append(f"  - Value Area: ${va_l}-${va_h}")
        price_in = session_ctx.get("price_in_value_area")
        if price_in is not None:
            parts.append(f"  - Price in VA: {price_in}")
    if "price_vs_poc" in session_ctx:
        parts.append(f"  - Price vs POC: {session_ctx['price_vs_poc']:+.0f}")
    ib_h = session_ctx.get("initial_balance_high")
    ib_l = session_ctx.get("initial_balance_low")
    if ib_h is not None:
        parts.append(f"  - Initial Balance: ${ib_l:.0f}-${ib_h:.0f}")
    drift = session_ctx.get("poc_drift_ratio")
    if drift is not None:
        drift_label = "range" if drift < 0.3 else "trend"
        parts.append(f"  - POC drift: {drift_label} ({drift:.2f})")
    anomaly = session_ctx.get("pre_session_anomaly")
    if anomaly:
        parts.append(f"  - Pre-session anomaly: {anomaly['anomaly_factor']}x baseline volume")
    return "\n".join(parts)


def _format_weekly(weekly_ctx: Dict[str, Any]) -> str:
    """Format weekly context."""
    if not weekly_ctx or weekly_ctx.get("week") in (None, "N/A"):
        return ""
    parts = [f"  - Week: {weekly_ctx['week']}"]
    poc = weekly_ctx.get("week_poc")
    if poc is not None:
        parts.append(f"  - Weekly POC: ${poc}")
    va_h = weekly_ctx.get("week_value_area_high")
    va_l = weekly_ctx.get("week_value_area_low")
    if va_h is not None and va_l is not None:
        price_in = weekly_ctx.get("price_in_week_value_area")
        in_str = " (in VA)" if price_in else " (outside VA)"
        parts.append(f"  - Weekly VA: ${va_l}-${va_h}{in_str}")
    return "\n".join(parts)


def _format_shape(shape_ctx: Dict[str, Any]) -> str:
    """Format profile shape."""
    shape = shape_ctx.get("shape", "unknown")
    if shape == "unknown":
        return ""
    descs = {"P": "Short-covering top", "b": "Distribution bottom",
             "D": "Balance / single acceptance", "B": "Double distribution"}
    desc = descs.get(shape, "")
    return f"  - Profile shape: {shape} ({desc})"


def _format_composite(composite_ctx: Dict[str, Any]) -> str:
    """Format composite context (HVN/LVN zones, balance/imbalance)."""
    if not composite_ctx or composite_ctx.get("profile_count", 0) < 2:
        return ""
    parts = [f"  - Composite: {composite_ctx['profile_count']} profiles"]
    poc = composite_ctx.get("composite_poc")
    if poc is not None:
        parts.append(f"  - Composite POC: ${poc}")
    bal = composite_ctx.get("balance_flag", "")
    if bal not in ("insufficient_data",):
        parts.append(f"  - Balance/Imbalance: {bal}")
    hvn = composite_ctx.get("hvn_zones", [])
    if hvn:
        # Summarise strongest HVN
        top = max(hvn, key=lambda z: z["volume"])
        parts.append(f"  - Strongest HVN: ${top['price_low']}-${top['price_high']} "
                     f"(vol={top['volume']:.0f})")
    lvn = composite_ctx.get("lvn_zones", [])
    if lvn:
        top_lvn = max(lvn, key=lambda z: z["volume"])
        parts.append(f"  - Notable LVN: ${top_lvn['price_low']}-${top_lvn['price_high']}")
    return "\n".join(parts)


def _format_divergence(div: Optional[Dict[str, Any]]) -> str:
    if not div:
        return ""
    dt = div.get("divergence_type", "")
    label = dt.replace("_", " ").title()
    return f"  - CVD Divergence: {label} (price={div.get('price'):.0f}, cvd={div.get('cvd_delta'):+.2f})"


def _build_prompt(snapshots: list, patterns: list) -> str:
    latest = snapshots[-1]
    prev = snapshots[0] if len(snapshots) > 1 else latest

    cvd = latest.get("cvd", {})
    prev_cvd = prev.get("cvd", {})

    mid = latest.get("mid_price")
    spread = latest.get("spread")
    imb5 = latest.get("imbalance_5")
    imb20 = latest.get("imbalance_20")
    rolling = cvd.get("rolling_delta", 0)
    cum_delta = cvd.get("cumulative_delta", 0)
    prev_rolling = prev_cvd.get("rolling_delta", 0)
    ratio = cvd.get("cvd_ratio", 0)

    bids = latest.get("bids", [])[:5]
    asks = latest.get("asks", [])[:5]
    bid_str = "  ".join(f"${p:.0f}x{s:.3f}" for p, s in bids) if bids else "n/a"
    ask_str = "  ".join(f"${p:.0f}x{s:.3f}" for p, s in asks) if asks else "n/a"

    # Pattern block
    pattern_block = ""
    if patterns:
        lines = []
        for p in patterns:
            bucket = p.get("price_bucket", "?")
            z = p.get("z_score", 0)
            vol = p.get("volume", 0)
            absorp = p.get("absorption", False)
            tag = " [ABSORPTION]" if absorp else ""
            lines.append(f"  - Bucket ${bucket}: z {z:.2f}, vol {vol:.4f}{tag}")
        pattern_block = (
            "\nERKANNTE MUSTER:\n" + "\n".join(lines[-PATTERN_CONTEXT_SIZE:])
        )

    # Context layer blocks
    session_block = _format_session(latest.get("session_context", {}))
    weekly_block = _format_weekly(latest.get("weekly_context", {}))
    shape_block = _format_shape(latest.get("profile_shape", {}))
    composite_block = _format_composite(latest.get("composite_context", {}))
    divergence_block = _format_divergence(latest.get("divergence"))

    context_sections = "\n".join(
        s for s in [session_block, weekly_block, shape_block, composite_block,
                    divergence_block] if s
    )

    return (
        "Du bist ein erfahrener BTC Order Flow Analyst. Analysiere die folgenden "
        f"Live-Marktdaten und gib ein praezises Signal (max 2 Saetze).\n"
        f"\n"
        f"PREIS & LIQUIDITAET:\n"
        f"- Preis: ${mid:.2f} | Spread: ${spread:.2f}\n"
        f"- Imbalance Top-5: {imb5:+.3f} | Top-20: {imb20:+.3f}\n"
        f"- CVD Rolling (200): {rolling:+.2f} BTC (vorher: {prev_rolling:+.2f})\n"
        f"- CVD Kumulativ: {cum_delta:+.2f} BTC | Ratio: {ratio:+.3f}\n"
        f"\n"
        f"TOP BIDS: {bid_str}\n"
        f"TOP ASKS: {ask_str}\n"
        f"{pattern_block}\n"
        f"\n"
        f"MARKTSTRUKTUR-KONTEXT:\n"
        f"{context_sections}\n"
        f"\n"
        f"Volumen-Signifikanz wurde bereits deterministisch berechnet. "
        f"Ordne die Muster ein: Ist Absorption Akkumulation oder Distribution? "
        f"Passt die Divergenz zum CVD-Trend? Stimmt die Profil-Form mit "
        f"der aktuellen Preisrichtung ueberein?\n"
        f"\n"
        f'Format: "[BULLISH/BEARISH/NEUTRAL] - <Signal>".'
    )


async def run(broker: Broker, shutdown: asyncio.Event) -> None:
    api_key = _load_api_key()
    if not api_key:
        logger.warning("DEEPSEEK_API_KEY not found -- Signal Agent disabled.")
        await broker.publish(SIGNALS, {
            "timestamp": int(time.time() * 1000),
            "text": "WARNING Signal Agent inactive -- DEEPSEEK_API_KEY missing in .env",
            "level": "warning",
        })
        return

    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
    agg_q = broker.subscribe(AGGREGATED)
    pattern_q = broker.subscribe(PATTERNS)

    agg_history: deque = deque(maxlen=HISTORY_SIZE)
    pattern_history: deque = deque(maxlen=PATTERN_CONTEXT_SIZE)
    last_signal_at: float = 0.0

    logger.info("Signal Agent started (interval: %ds).", int(SIGNAL_INTERVAL))

    while not shutdown.is_set():
        try:
            msg = await asyncio.wait_for(agg_q.get(), timeout=1.0)
            agg_history.append(msg)

            while True:
                try:
                    pat = pattern_q.get_nowait()
                    pattern_history.append(pat)
                except asyncio.QueueEmpty:
                    break

            now = time.time()
            if len(agg_history) < 3:
                continue
            if now - last_signal_at < SIGNAL_INTERVAL:
                continue

            last_signal_at = now
            snaps = list(agg_history)
            patterns = list(pattern_history)

            signal_text = await asyncio.get_event_loop().run_in_executor(
                None, _call_deepseek, client, snaps, patterns
            )

            if signal_text:
                await broker.publish(SIGNALS, {
                    "timestamp": int(time.time() * 1000),
                    "text": signal_text,
                    "level": "info",
                })
                logger.info("Signal: %s", signal_text)

        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.error("Signal Agent error: %s", e)
            await asyncio.sleep(5)


def _call_deepseek(client: OpenAI, snapshots: list, patterns: list) -> Optional[str]:
    try:
        prompt = _build_prompt(snapshots, patterns)
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error("DeepSeek API error: %s", e)
        return None
