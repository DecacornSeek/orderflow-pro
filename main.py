"""
main.py — Orchestrator fuer OrderFlow Pro Agenten.

Initialisiert den In-Process Message Broker und startet alle Agent-Loops
(inkl. OptionsAgent fuer Deribit GEX und Strukturdaten).
"""

import asyncio
import logging
import signal
import sys

from core.broker import Broker
from agents import options_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("orderflow-pro.main")


async def main() -> None:
    broker = Broker()
    shutdown_event = asyncio.Event()

    # Graceful shutdown handler
    def handle_signal() -> None:
        logger.info("Shutdown-Signal empfangen, stoppe Agenten...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            # Fallback fuer Plattformen ohne add_signal_handler (z.B. Windows)
            pass

    logger.info("Starte OrderFlow Pro Agenten...")

    # Tasks fuer alle registrierten Agenten
    tasks = [
        asyncio.create_task(options_agent.run(broker, shutdown_event), name="OptionsAgent"),
    ]

    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Alle Agenten ordnungsgemaess beendet.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
