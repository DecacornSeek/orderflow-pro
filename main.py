import asyncio
import logging
import signal as _signal
import webbrowser
import threading

import ccxt

from core.broker import Broker
from core.history import History
from agents import exchange_agent, aggregator_agent, signal_agent
from agents.display_agent import DisplayAgent
from agents.logger_agent import LoggerAgent

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _fetch_klines() -> list:
    """500 historische 1m-Kerzen von Binance REST holen."""
    ex = ccxt.binance()
    raw = ex.fetch_ohlcv("BTC/USDT", "1m", limit=500)
    return [
        {"time": ts // 1000, "open": o, "high": h, "low": l, "close": c, "volume": v}
        for ts, o, h, l, c, v in raw
    ]


async def main() -> None:
    broker   = Broker()
    history  = History()
    shutdown = asyncio.Event()

    # Klines synchron holen bevor Agents starten
    print("Lade historische Kerzen von Binance...")
    try:
        klines = await asyncio.get_event_loop().run_in_executor(None, _fetch_klines)
        history.set_klines(klines)
        print(f"  {len(klines)} Kerzen geladen.")
    except Exception as e:
        print(f"  Klines nicht verfügbar: {e}")

    # Signal Handler (Ctrl+C)
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            pass  # Windows

    display = DisplayAgent(broker, history)
    logger_agent = LoggerAgent(broker)

    # Browser nach kurzem Delay öffnen
    threading.Timer(2.0, lambda: webbrowser.open("http://localhost:8000")).start()

    print("Dashboard: http://localhost:8000")
    print("Daten werden gespeichert in: data/")
    print("Strg+C zum Beenden.\n")

    tasks = [
        asyncio.create_task(exchange_agent.run(broker, shutdown),    name="exchange"),
        asyncio.create_task(aggregator_agent.run(broker, shutdown),  name="aggregator"),
        asyncio.create_task(signal_agent.run(broker, shutdown),      name="signal"),
        asyncio.create_task(display.run(shutdown),                    name="display"),
        asyncio.create_task(logger_agent.run(shutdown),               name="logger"),
    ]

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        shutdown.set()
    finally:
        shutdown.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
