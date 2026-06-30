import asyncio
import logging
import os
from typing import List

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from core.broker import Broker, AGGREGATED, SIGNALS, TRADES
from core.history import History
import core.metrics as _metrics

logger = logging.getLogger(__name__)
STATIC = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")


class DisplayAgent:
    def __init__(self, broker: Broker, history: History) -> None:
        self.broker = broker
        self.history = history
        self._connections: List[WebSocket] = []
        self._app = self._build_app()

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/")
        async def index():
            with open(STATIC, encoding="utf-8") as f:
                return HTMLResponse(f.read())

        @app.get("/history")
        async def get_history():
            return {
                "klines": self.history.get_klines(),
                "vap":    self.history.get_vap(),
            }

        @app.get("/metrics")
        async def get_metrics():
            return _metrics.snapshot()

        @app.get("/depth-history")
        async def depth_history(last_n: int = 60):
            """Return the last N aggregator snapshots with bid/ask depth.

            Each frame: {timestamp, bids: [[price, size], ...], asks: [[price, size], ...]}.
            Price levels are sorted: bids descending, asks ascending.

            Query params:
                last_n (int, default 60): Number of frames to return.

            Returns:
                JSON list of depth frames, newest last.
            """
            return self.history.get_depth_frames(last_n)

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket):
            await websocket.accept()
            self._connections.append(websocket)
            # Sofort VAP-Stand schicken damit Volume Profile direkt sichtbar ist
            await websocket.send_json({
                "type": "history",
                "vap": self.history.get_vap(),
            })
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                self._connections.remove(websocket)

        return app

    async def _broadcast(self, data: dict) -> None:
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self._connections:
                self._connections.remove(ws)

    async def _pump(self, shutdown: asyncio.Event) -> None:
        agg_q   = self.broker.subscribe(AGGREGATED)
        sig_q   = self.broker.subscribe(SIGNALS)
        trade_q = self.broker.subscribe(TRADES)

        async def _drain_agg() -> None:
            while True:
                msg = await agg_q.get()
                self.history.add_snapshot(msg)
                await self._broadcast({"type": "tick", **msg})

        async def _drain_trades() -> None:
            while True:
                msg = await trade_q.get()
                price = msg.get("price")
                size  = msg.get("size")
                if price and size:
                    self.history.add_trade(float(price), float(size))
                await self._broadcast({"type": "trade", **msg})

        async def _drain_signals() -> None:
            while True:
                msg = await sig_q.get()
                await self._broadcast({"type": "signal", **msg})

        drain_tasks = [
            asyncio.create_task(_drain_agg()),
            asyncio.create_task(_drain_trades()),
            asyncio.create_task(_drain_signals()),
        ]
        await shutdown.wait()
        for t in drain_tasks:
            t.cancel()
        await asyncio.gather(*drain_tasks, return_exceptions=True)

    async def run(self, shutdown: asyncio.Event) -> None:
        config = uvicorn.Config(
            self._app, host="127.0.0.1", port=8000, log_level="warning"
        )
        server = uvicorn.Server(config)

        pump_task   = asyncio.create_task(self._pump(shutdown))
        server_task = asyncio.create_task(server.serve())

        logger.warning("Dashboard: http://127.0.0.1:8000")

        await shutdown.wait()
        server.should_exit = True
        await asyncio.gather(pump_task, server_task, return_exceptions=True)
