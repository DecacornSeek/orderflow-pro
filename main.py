"""
main.py — Orchestrator fuer OrderFlow Pro.

Startet den In-Process Broker und alle Agent-Loops. Es gibt genau einen
Datenpfad: Exchange -> Aggregator -> Display, plus den Options-Agent, der
die Deribit-Kette in denselben Broker publiziert (Charter §2, kein
paralleler Stack).

Aufruf:
    python3 main.py            # Dashboard auf http://127.0.0.1:8000
                               # Risikoblatt auf http://127.0.0.1:8000/risk
"""

import asyncio
import logging
import signal as _signal
import threading
import webbrowser

from agents import aggregator_agent, exchange_agent, options_agent
from agents.display_agent import DisplayAgent
from agents.logger_agent import LoggerAgent
from core.broker import Broker
from core.history import History

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("orderflow-pro.main")

DASHBOARD_URL = "http://127.0.0.1:8000"


def _fetch_klines() -> list:
    """500 historische 1m-Kerzen von Binance REST holen."""
    import ccxt

    ex = ccxt.binance()
    raw = ex.fetch_ohlcv("BTC/USDT", "1m", limit=500)
    return [
        {"time": ts // 1000, "open": o, "high": h, "low": low, "close": c, "volume": v}
        for ts, o, h, low, c, v in raw
    ]


async def main() -> None:
    broker = Broker()
    history = History()
    shutdown = asyncio.Event()

    # Kerzen vorab laden — ohne sie ist keine realisierte Volatilitaet rechenbar.
    print("Lade historische Kerzen von Binance...")
    try:
        klines = await asyncio.get_running_loop().run_in_executor(None, _fetch_klines)
        history.set_klines(klines)
        print(f"  {len(klines)} Kerzen geladen.")
    except Exception as exc:
        # Kein Abbruch: die Agenten laufen weiter, RV bleibt bis zum Auffuellen
        # des Ringpuffers schlicht unbekannt und wird als solche angezeigt.
        print(f"  Kerzen nicht verfuegbar ({exc}) — RV bleibt vorerst unbekannt.")

    loop = asyncio.get_running_loop()

    def _request_shutdown(sig=None, frame=None) -> None:
        loop.call_soon_threadsafe(shutdown.set)

    for sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except (NotImplementedError, ValueError):
            # Windows kennt add_signal_handler fuer SIGTERM nicht
            _signal.signal(sig, _request_shutdown)

    display = DisplayAgent(broker, history)
    logger_agent = LoggerAgent(broker)

    threading.Timer(2.0, lambda: webbrowser.open(DASHBOARD_URL)).start()

    print(f"Dashboard:   {DASHBOARD_URL}")
    print(f"Risikoblatt: {DASHBOARD_URL}/risk")
    print("Daten werden gespeichert in: data/")
    print("Strg+C zum Beenden.\n")

    tasks = [
        asyncio.create_task(exchange_agent.run(broker, shutdown), name="exchange"),
        asyncio.create_task(aggregator_agent.run(broker, shutdown), name="aggregator"),
        asyncio.create_task(options_agent.run(broker, shutdown), name="options"),
        asyncio.create_task(display.run(shutdown), name="display"),
        asyncio.create_task(logger_agent.run(shutdown), name="logger"),
        asyncio.create_task(shutdown.wait(), name="shutdown"),
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

    logger.info("Alle Agenten ordnungsgemaess beendet.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
