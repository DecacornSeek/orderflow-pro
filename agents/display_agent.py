import asyncio
from datetime import datetime, timezone
import logging
import math
import os
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents.options_agent import snapshot_to_dict
from core.broker import Broker, AGGREGATED, OPTIONS, SIGNALS, TRADES
from core.event_layer import build_events, events_to_dict
from core.history import History
import core.metrics as _metrics
from core.regime_state import ChangeLog, RegimeTracker, SessionAnchor
from core.session_corridor import build_corridor, corridor_to_dict, target_position
from strategies.base import (
    PROP_FIRM_PRESETS,
    RiskGuardrails,
    rules_to_dict,
    simulate_challenge,
    size_position,
)
from strategies.geometry import evaluate_geometry, realised_vol_annualised

logger = logging.getLogger(__name__)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
STATIC = os.path.join(STATIC_DIR, "index.html")
RISK_PAGE = os.path.join(STATIC_DIR, "risk.html")


# Charter §7: Jede angezeigte Zahl traegt eine Annahme. Diese vier muessen
# fuer den Nutzer sichtbar sein, sonst wird die Karte ueberinterpretiert.
# Sie stehen hier und nicht im HTML, damit sie nur einmal existieren.
LIMITS = [
    {
        "key": "terminal_containment",
        "titel": "68% ist Terminal-Containment, keine Beruehrungsgrenze",
        "text": "Der Anteil, der zum Reset im Band endet. Die Wahrscheinlichkeit, "
                "das obere 1-Sigma-Band waehrend der Session zu beruehren, liegt "
                "nach dem Reflexionsprinzip bei rund 32%.",
    },
    {
        "key": "dealer_side",
        "titel": "Das GEX-Vorzeichen haengt an einer Annahme ueber die Halterseite",
        "text": "Bei BTC steht Covered-Call-Writing von Minern und Treasuries gegen "
                "Retail-Call-Buying. Das Vorzeichen ist schwaecher bestimmt als bei "
                "SPX. Der Flip ist eine Orientierungsmarke, kein Schalter.",
    },
    {
        "key": "walls",
        "titel": "Walls markieren, wo Hedging klebt — nicht, wo der Preis dreht",
        "text": "Eine Wall ist die groesste Gamma-Konzentration, keine Aussage "
                "darueber, dass der Preis dort umkehrt.",
    },
    {
        "key": "gamma_floor",
        "titel": "Gamma-Floor bei T gegen 0",
        "text": "Ohne Floor springt die Anzeige kurz vor Settlement in einen "
                "Extremzustand, der ein Artefakt der Formel ist. Gerechnet wird "
                "mit mindestens 15 Minuten Restlaufzeit.",
    },
]


class EvaluateRequest(BaseModel):
    """Eingabe fuer /risk/evaluate — Barrieren setzt der Trader, nicht das System."""

    entry: float
    stop: float
    target: float
    rules_key: str = "breakout_10k"
    risk_pct: float = 0.005
    annual_vol: Optional[float] = None
    annual_drift: float = 0.0
    cost_r: float = 0.04
    manual_win_rate: Optional[float] = None
    trades_per_day: int = 3


class DisplayAgent:
    def __init__(self, broker: Broker, history: History) -> None:
        self.broker = broker
        self.history = history
        self._connections: List[WebSocket] = []
        # Letzter bekannter Zustand je Kanal. /risk/state liest daraus, statt
        # eigene Engines zu halten — es gibt genau einen Datenpfad (Charter §2).
        self._last_aggregated: Optional[Dict[str, Any]] = None
        self._last_options: Optional[Dict[str, Any]] = None

        # Charter §6: Stabilitaet der Anzeige. Die Zustandsautomaten laufen im
        # Broker-Loop, nicht im HTTP-Handler — sonst wuerde die Poll-Rate des
        # Browsers die Verweildauer der Hysterese bestimmen.
        self._regime = RegimeTracker()
        self._anchor = SessionAnchor()
        self._changes = ChangeLog()
        self._regime_state = self._regime.update(None)
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

        @app.get("/risk")
        async def risk_page():
            with open(RISK_PAGE, encoding="utf-8") as f:
                return HTMLResponse(f.read())

        @app.get("/risk/state")
        async def risk_state():
            return self._build_risk_state()

        @app.post("/risk/evaluate")
        async def risk_evaluate(req: EvaluateRequest):
            return self._evaluate(req)

        @app.get("/options")
        async def options_snapshot():
            """
            Letzter Options-Snapshot. None bedeutet: keine belastbare Kette —
            die Anzeige bleibt leer, statt alte Zahlen weiterzureichen.
            """
            return {"options": self._last_options}

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

    # ── Risikoblatt-Zustand ──────────────────────────────────────────────────

    def _realised_vol(self) -> Optional[float]:
        """
        Annualisierte RV aus den Minutenkerzen. None bei zu wenig Historie —
        die frueheren Fassungen setzten hier 0.52 ein, was sich hinterher nicht
        mehr von einer gemessenen RV unterscheiden liess (Charter §2).
        """
        klines = self.history.get_klines()
        if len(klines) < 10:
            return None
        returns = []
        for prev, cur in zip(klines, klines[1:]):
            prev_close, cur_close = prev.get("close"), cur.get("close")
            if prev_close and cur_close and prev_close > 0 and cur_close > 0:
                returns.append(math.log(cur_close / prev_close))
        return realised_vol_annualised(returns)

    def _build_risk_state(self) -> Dict[str, Any]:
        """
        Setzt den Zustand fuer die Pre-Session-Karte zusammen.

        Jedes Feld ist entweder gemessen oder None. Es gibt keinen Zweig, der
        einen Ersatzwert einsetzt, damit die Karte vollstaendig aussieht.
        """
        agg = self._last_aggregated
        options = self._last_options
        rv = self._realised_vol()

        spot = agg.get("mid_price") if agg else None
        session_ctx = (agg or {}).get("session_context") or {}

        iv_atm = options.get("atm_iv") if options else None
        iv_rv_ratio = None
        if iv_atm is not None and rv is not None and rv > 0.0:
            iv_rv_ratio = round(iv_atm / rv, 2)

        return {
            "timestamp": (agg or {}).get("timestamp"),
            "spot": spot,
            "spread": (agg or {}).get("spread"),
            "orderbook": {
                "bids": (agg or {}).get("bids", []),
                "asks": (agg or {}).get("asks", []),
                "imbalance_5": (agg or {}).get("imbalance_5"),
                "imbalance_20": (agg or {}).get("imbalance_20"),
            } if agg else None,
            "options": options,
            "volatility": {
                "realised_vol_annualised": rv,
                "atm_iv": iv_atm,
                "iv_rv_ratio": iv_rv_ratio,
            },
            "cvd": (agg or {}).get("cvd"),
            "session_context": session_ctx,
            "weekly_context": (agg or {}).get("weekly_context"),
            "road_map": (agg or {}).get("road_map"),
            "business_zones": (agg or {}).get("business_zones"),
            "volume_profile": {
                "vap": self.history.get_vap(),
                "poc": session_ctx.get("session_poc"),
                "vah": session_ctx.get("session_value_area_high"),
                "val": session_ctx.get("session_value_area_low"),
            },
            # Charter §4.1 verlangt echte Liquidationscluster aus Perp-OI.
            # Die frueheren Hebel-Stufen um den Spot herum waren eine
            # geometrische Konstruktion, kein beobachteter Cluster.
            "liquidations": None,
            "liquidations_note": "Noch nicht verfuegbar — verlangt Perp-OI je Preisniveau.",
            "presets": {k: rules_to_dict(r) for k, r in PROP_FIRM_PRESETS.items()},
            "data_ready": agg is not None,
            **self._build_context(),
        }

    def _evaluate(self, req: "EvaluateRequest") -> Dict[str, Any]:
        """
        Wertet eine vom Trader gesetzte Geometrie aus. Das System schlaegt
        keine Barrieren vor (Charter §2) — es rechnet die gesetzten durch.
        """
        if not (req.stop < req.entry < req.target):
            return {
                "error": "Stop muss unter Entry liegen, Target darueber.",
                "entry": req.entry, "stop": req.stop, "target": req.target,
            }

        annual_vol = req.annual_vol if req.annual_vol is not None else self._realised_vol()
        if annual_vol is None or annual_vol <= 0.0:
            return {
                "error": "Keine Volatilitaet verfuegbar — zu wenig Kurshistorie. "
                         "Entweder warten oder annual_vol explizit setzen.",
                "annual_vol": None,
            }

        rules = PROP_FIRM_PRESETS.get(req.rules_key) or PROP_FIRM_PRESETS["breakout_10k"]

        geometry = evaluate_geometry(
            entry=req.entry, stop=req.stop, target=req.target,
            annual_vol=annual_vol, annual_drift=req.annual_drift,
            spot=req.entry, cost_r=req.cost_r,
        )

        manual = None
        if req.manual_win_rate is not None:
            p = req.manual_win_rate
            ev = p * geometry.rrr - (1.0 - p) - req.cost_r
            manual = {
                "manual_p_win": p,
                "manual_expectancy_r": round(ev, 4),
                "is_positive": ev > 0.0,
            }

        stop_distance_pct = abs(req.entry - req.stop) / req.entry
        sizing = size_position(
            rules=rules,
            stop_distance_pct=stop_distance_pct,
            guardrails=RiskGuardrails(risk_per_trade_pct=req.risk_pct),
            btc_price=req.entry,
        )

        p_win = req.manual_win_rate if req.manual_win_rate is not None else geometry.p_target
        simulation = simulate_challenge(
            rules=rules,
            p_win=p_win,
            rrr=geometry.rrr,
            risk_usd=sizing.max_loss_if_stopped,
            cost_usd=sizing.commission_rt,
            trades_per_day=req.trades_per_day,
        )

        return {
            "geometry": {**geometry.__dict__, "manual_evaluation": manual},
            "sizing": sizing.__dict__,
            "simulation": simulation.__dict__,
            "rules": rules_to_dict(rules),
            "annual_vol_source": "gemessen" if req.annual_vol is None else "vorgegeben",
            "caveat": (
                "Ohne Drift ist E[R] ueber alle gueltigen Entries konstant -cost_r: "
                "die Geometrie allein erzeugt keinen Edge. p_timeout ist 0 — der "
                "Horizont ist unendlich, offene Trades zum Sessionende fehlen im Modell."
            ),
        }

    # ── Zustandsfortschreibung (Charter §6) ─────────────────────────────────

    def _advance_state(self, options: Dict[str, Any]) -> None:
        """
        Schreibt Regime, Anker und Aenderungsliste fort. Wird pro
        Options-Snapshot aufgerufen, nicht pro HTTP-Abruf.
        """
        vorher = self._regime_state.state
        self._regime_state = self._regime.update(options.get("net_gex_usd"))
        if self._regime_state.state != vorher and self._regime_state.is_committed:
            self._changes.record(
                "regime",
                f"Regime {vorher} -> {self._regime_state.state}",
                dedupe_key=self._regime_state.state,
            )

        self._anchor.update({
            "spot": options.get("spot"),
            "net_gex_usd": options.get("net_gex_usd"),
            "zero_gamma": options.get("zero_gamma"),
            "put_wall": options.get("put_wall"),
            "call_wall": options.get("call_wall"),
            "atm_iv": options.get("atm_iv"),
        })

        # Charter §4.2: nur die informativen Verschiebungen melden. Eine
        # Bewegung, die vom Spot kommt, gehoert nicht in die Aenderungsliste —
        # sonst ist die Liste voll und die eine Meldung, die zaehlt, geht unter.
        # Faellt 0DTE, Weekly und Monthly auf denselben Termin (jeder letzte
        # Freitag im Monat), zeigen alle drei dieselbe Verschiebung. Gemeldet
        # wird sie einmal, sonst flutet ein Ereignis die Liste dreifach.
        gemeldet = set()
        for shift in options.get("chain_shift", []):
            if not shift.get("is_informative"):
                continue
            termin = shift.get("expiry")
            if termin in gemeldet:
                continue
            gemeldet.add(termin)
            label = shift.get("label", "?")
            teile = []
            zg = shift.get("zero_gamma_informative")
            if zg:
                teile.append(f"Zero-Gamma {zg:+,.0f} bei stehendem Spot")
            if shift.get("call_wall_moved"):
                teile.append("Call-Wall gewandert")
            if shift.get("put_wall_moved"):
                teile.append("Put-Wall gewandert")
            if teile:
                self._changes.record(
                    f"chain_{label}",
                    f"{label.upper()}: " + ", ".join(teile),
                    dedupe_key="|".join(teile),
                )

    def _build_context(self) -> Dict[str, Any]:
        """Korridor, Ereignisse, Regime und Anker fuer die Pre-Session-Karte."""
        options = self._last_options or {}
        agg = self._last_aggregated or {}
        now = datetime.now(timezone.utc)

        spot = agg.get("mid_price") or options.get("spot")
        corridor = build_corridor(spot, options.get("atm_iv"), now)
        events = build_events(now)

        rs = self._regime_state
        aktuell = {
            "spot": spot,
            "net_gex_usd": options.get("net_gex_usd"),
            "zero_gamma": options.get("zero_gamma"),
            "put_wall": options.get("put_wall"),
            "call_wall": options.get("call_wall"),
            "atm_iv": options.get("atm_iv"),
        }

        return {
            "corridor": corridor_to_dict(corridor),
            "events": events_to_dict(events),
            "regime": {
                "state": rs.state,
                "raw_state": rs.raw_state,
                "band": rs.band,
                "pending_state": rs.pending_state,
                "pending_seconds": round(rs.pending_seconds, 1),
                "dwell_required": rs.dwell_required,
                "changed_at": rs.changed_at.isoformat() if rs.changed_at else None,
                "is_committed": rs.is_committed,
            },
            "session_anchor": self._anchor.to_dict(aktuell),
            "changes": self._changes.entries(),
            "chain_shift": options.get("chain_shift", []),
            "limits": LIMITS,
        }

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
        opt_q   = self.broker.subscribe(OPTIONS)

        async def _drain_agg() -> None:
            while True:
                msg = await agg_q.get()
                self.history.add_snapshot(msg)
                self._last_aggregated = msg
                await self._broadcast({"type": "tick", **msg})

        async def _drain_options() -> None:
            while True:
                snap = await opt_q.get()
                payload = snapshot_to_dict(snap)
                self._last_options = payload
                self._advance_state(payload)
                await self._broadcast({"type": "options", **payload})

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
            asyncio.create_task(_drain_options()),
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
