import asyncio
import logging
import os
import time
from collections import deque
from typing import Optional

from openai import OpenAI

from core.broker import Broker, AGGREGATED, SIGNALS

logger = logging.getLogger(__name__)

SIGNAL_INTERVAL = 15.0  # API call alle 15 Sekunden
HISTORY_SIZE = 10       # letzte N snapshots für Kontext
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


def _build_prompt(snapshots: list) -> str:
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

    bid_str = "  ".join(f"${p:.0f}×{s:.3f}" for p, s in bids) if bids else "n/a"
    ask_str = "  ".join(f"${p:.0f}×{s:.3f}" for p, s in asks) if asks else "n/a"

    return f"""Du bist ein erfahrener BTC Order Flow Analyst. Analysiere die folgenden Live-Marktdaten und gib ein präzises, kurzes Signal (max 2 Sätze).

AKTUELLE MARKTDATEN (Binance BTC/USDT):
- Preis: ${mid:.2f} | Spread: ${spread:.2f}
- Bid Imbalance Top-5: {imb5:+.3f} | Top-20: {imb20:+.3f}  (+1 = pure bids, -1 = pure asks)
- CVD Rolling Delta (letzte 200 Trades): {rolling:+.2f} BTC  (vorher: {prev_rolling:+.2f})
- CVD Kumulativ: {cum_delta:+.2f} BTC
- Buy/Sell Ratio im Fenster: {ratio:+.3f}

TOP BIDS: {bid_str}
TOP ASKS: {ask_str}

Gib NUR das Signal aus — kein Intro, keine Erklärung der Methodik. Format: "[BULLISH/BEARISH/NEUTRAL] — <Signal>"."""


async def run(broker: Broker, shutdown: asyncio.Event) -> None:
    api_key = _load_api_key()
    if not api_key:
        logger.warning("DEEPSEEK_API_KEY nicht gefunden — Signal Agent deaktiviert.")
        await broker.publish(SIGNALS, {
            "timestamp": int(time.time() * 1000),
            "text": "⚠ Signal Agent inaktiv — DEEPSEEK_API_KEY fehlt in .env",
            "level": "warning",
        })
        return

    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
    agg_q = broker.subscribe(AGGREGATED)
    history: deque = deque(maxlen=HISTORY_SIZE)
    last_signal_at: float = 0.0

    logger.info("Signal Agent gestartet (Interval: %ds).", int(SIGNAL_INTERVAL))

    while not shutdown.is_set():
        try:
            msg = await asyncio.wait_for(agg_q.get(), timeout=1.0)
            history.append(msg)

            now = time.time()
            if len(history) < 3:
                continue
            if now - last_signal_at < SIGNAL_INTERVAL:
                continue

            last_signal_at = now
            snaps = list(history)

            # DeepSeek API in Thread damit asyncio nicht blockiert
            signal_text = await asyncio.get_event_loop().run_in_executor(
                None, _call_deepseek, client, snaps
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
            logger.error("Signal Agent Fehler: %s", e)
            await asyncio.sleep(5)


def _call_deepseek(client: OpenAI, snapshots: list) -> Optional[str]:
    try:
        prompt = _build_prompt(snapshots)
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error("DeepSeek API Fehler: %s", e)
        return None
