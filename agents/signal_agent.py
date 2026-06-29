"""Signal Agent — DeepSeek-powered market analysis with pattern context.

Subscribes to AGGREGATED snapshots and PATTERNS events from the Broker.
Every SIGNAL_INTERVAL seconds, builds a prompt from recent market data
(including any detected patterns from the PatternEngine) and calls
DeepSeek API for a concise market assessment.

The LLM does NOT guess whether volume is significant — the PatternEngine
has already determined that deterministically. The LLM only interprets
and contextualises the detected patterns.

Output is published on SIGNALS channel.
"""

import asyncio
import logging
import os
import time
from collections import deque
from typing import Optional

from openai import OpenAI

from core.broker import Broker, AGGREGATED, SIGNALS, PATTERNS

logger = logging.getLogger(__name__)

SIGNAL_INTERVAL = 15.0  # API call every 15 seconds
HISTORY_SIZE = 10       # last N snapshots for context
PATTERN_CONTEXT_SIZE = 5  # last N pattern hits to include
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


def _build_prompt(snapshots: list, patterns: list) -> str:
    """Build a prompt with market data + any detected patterns."""
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

    # Build patterns block
    pattern_block = ""
    if patterns:
        lines = []
        for p in patterns:
            bucket = p.get("price_bucket", "?")
            z = p.get("z_score", 0)
            vol = p.get("volume", 0)
            absorp = p.get("absorption", False)
            tag = " [ABSORPTION]" if absorp else ""
            lines.append(
                f"  - Bucket ${bucket}: z-score {z:.2f}, volume {vol:.4f}{tag}"
            )
        pattern_block = (
            "\nERKANNTE MUSTER (deterministisch):\n"
            + "\n".join(lines[-PATTERN_CONTEXT_SIZE:])
        )

    return (
        "Du bist ein erfahrener BTC Order Flow Analyst. Analysiere die folgenden "
        f"Live-Marktdaten und gib ein praezises, kurzes Signal (max 2 Saetze).\n"
        f"\n"
        f"AKTUELLE MARKTDATEN (Binance BTC/USDT):\n"
        f"- Preis: ${mid:.2f} | Spread: ${spread:.2f}\n"
        f"- Bid Imbalance Top-5: {imb5:+.3f} | Top-20: {imb20:+.3f}  "
        f"(+1 = pure bids, -1 = pure asks)\n"
        f"- CVD Rolling Delta (letzte 200 Trades): {rolling:+.2f} BTC  "
        f"(vorher: {prev_rolling:+.2f})\n"
        f"- CVD Kumulativ: {cum_delta:+.2f} BTC\n"
        f"- Buy/Sell Ratio im Fenster: {ratio:+.3f}\n"
        f"\n"
        f"TOP BIDS: {bid_str}\n"
        f"TOP ASKS: {ask_str}\n"
        f"{pattern_block}\n"
        f"HINWEIS: Volumen-Signifikanz und Absorption wurden bereits "
        f"deterministisch berechnet (siehe Erkannte Muster oben). Du musst "
        f"nicht selbst raten, ob ein Level signifikant ist. Ordne die Muster "
        f"stattdessen ein: Ist die Absorption ein Zeichen von Akkumulation "
        f"oder Distribution? Passt der z-Score zum aktuellen CVD-Trend?\n"
        f"\n"
        f'Gib NUR das Signal aus - kein Intro, keine Erklarung der Methodik. '
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
            # Wait for aggregated snapshot first, then drain patterns accumulated since
            msg = await asyncio.wait_for(agg_q.get(), timeout=1.0)
            agg_history.append(msg)

            # Drain patterns accumulated since last tick
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

            # DeepSeek API call in executor to avoid blocking asyncio
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
    """Call DeepSeek API with market data and pattern context."""
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
