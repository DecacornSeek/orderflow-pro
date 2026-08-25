"""
Logger Agent — speichert Live-Daten für späteres Training + Market Structure Context.

Schreibt:
  data/trades_YYYY-MM-DD.parquet          — rohe Trades
  data/snapshots_YYYY-MM-DD.parquet       — CVD + L2 + Context Layer Metriken
  data/signals_YYYY-MM-DD.jsonl           — Signal + Context (LLM Training)

Die Context-Layer-Felder (session, weekly, shape, composite, divergence) werden
sowohl in Snapshots als auch in Signal-JSONL gespeichert, damit sie für
nachgelagerte Analyse und Labeling zur Verfügung stehen.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd

from core.broker import Broker, TRADES, AGGREGATED, SIGNALS

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
FLUSH_INTERVAL = 60
BUFFER_MAX     = 1000


class LoggerAgent:

    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self._trade_buf:    List[dict] = []
        self._snapshot_buf: List[dict] = []
        self._last_snapshot: dict = {}
        DATA_DIR.mkdir(exist_ok=True)

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _ts_to_hms(ts_ms: Optional[int]) -> str:
        if ts_ms is None:
            return "--:--:--"
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")

    def _parquet_append(self, path: Path, new_rows: List[dict]) -> None:
        new_df = pd.DataFrame(new_rows)
        if path.exists():
            existing = pd.read_parquet(path)
            df = pd.concat([existing, new_df], ignore_index=True)
        else:
            df = new_df
        df.to_parquet(path, index=False, compression="snappy")

    def _flush_trades(self) -> None:
        if not self._trade_buf:
            return
        path = DATA_DIR / f"trades_{self._today()}.parquet"
        self._parquet_append(path, self._trade_buf)
        logger.debug("Trades gespeichert: %d -> %s", len(self._trade_buf), path.name)
        self._trade_buf.clear()

    def _flush_snapshots(self) -> None:
        if not self._snapshot_buf:
            return
        path = DATA_DIR / f"snapshots_{self._today()}.parquet"
        self._parquet_append(path, self._snapshot_buf)
        logger.debug("Snapshots gespeichert: %d -> %s", len(self._snapshot_buf), path.name)
        self._snapshot_buf.clear()

    def _log_signal(self, signal_msg: dict) -> None:
        snap = self._last_snapshot
        cvd  = snap.get("cvd", {})

        # Extract context layer fields (flattened for JSONL)
        session_ctx = snap.get("session_context", {})
        weekly_ctx = snap.get("weekly_context", {})
        shape_ctx = snap.get("profile_shape", {})
        composite_ctx = snap.get("composite_context", {})
        divergence = snap.get("divergence")

        record = {
            "timestamp": signal_msg.get("timestamp"),
            "signal": signal_msg.get("text", ""),
            "level": signal_msg.get("level", "info"),
            "price_at_signal": snap.get("mid_price"),
            "context": {
                "mid_price":        snap.get("mid_price"),
                "spread":           snap.get("spread"),
                "imbalance_5":      snap.get("imbalance_5"),
                "imbalance_20":     snap.get("imbalance_20"),
                "rolling_delta":    cvd.get("rolling_delta"),
                "cumulative_delta": cvd.get("cumulative_delta"),
                "cvd_ratio":        cvd.get("cvd_ratio"),
                "trade_count":      cvd.get("trade_count"),
                "bids_top5":        snap.get("bids", [])[:5],
                "asks_top5":        snap.get("asks", [])[:5],
                # Context Layer
                "session":          session_ctx.get("session"),
                "session_poc":      session_ctx.get("session_poc"),
                "session_va_high":  session_ctx.get("session_value_area_high"),
                "session_va_low":   session_ctx.get("session_value_area_low"),
                "price_in_va":      session_ctx.get("price_in_value_area"),
                "price_vs_poc":     session_ctx.get("price_vs_poc"),
                "initial_balance_high": session_ctx.get("initial_balance_high"),
                "initial_balance_low":  session_ctx.get("initial_balance_low"),
                "poc_drift_ratio":  session_ctx.get("poc_drift_ratio"),
                "pre_session_anomaly": session_ctx.get("pre_session_anomaly"),
                "week":             weekly_ctx.get("week"),
                "week_poc":         weekly_ctx.get("week_poc"),
                "week_va_high":     weekly_ctx.get("week_value_area_high"),
                "week_va_low":      weekly_ctx.get("week_value_area_low"),
                "price_in_week_va": weekly_ctx.get("price_in_week_value_area"),
                "profile_shape":    shape_ctx.get("shape"),
                "composite_poc":    composite_ctx.get("composite_poc"),
                "composite_balance_flag": composite_ctx.get("balance_flag"),
                "composite_hvn_count":    len(composite_ctx.get("hvn_zones", [])),
                "composite_lvn_count":    len(composite_ctx.get("lvn_zones", [])),
                "divergence_type":  divergence.get("divergence_type") if divergence else None,
            },
            "price_5min_later":  None,
            "price_15min_later": None,
            "price_30min_later": None,
        }

        path = DATA_DIR / f"signals_{self._today()}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        ts = self._ts_to_hms(record["timestamp"])
        logger.warning("Signal geloggt [%s]: %s", ts, record["signal"][:60])

    async def run(self, shutdown: asyncio.Event) -> None:
        trade_q    = self.broker.subscribe(TRADES)
        snapshot_q = self.broker.subscribe(AGGREGATED)
        signal_q   = self.broker.subscribe(SIGNALS)

        last_flush = asyncio.get_event_loop().time()
        logger.warning("Logger Agent gestartet -> %s", DATA_DIR)

        while not shutdown.is_set():
            while True:
                try:
                    msg = trade_q.get_nowait()
                    self._trade_buf.append({
                        "timestamp": msg.get("timestamp"),
                        "price":     msg.get("price"),
                        "size":      msg.get("size"),
                        "side":      msg.get("side"),
                        "exchange":  msg.get("exchange", "binance"),
                    })
                except asyncio.QueueEmpty:
                    break

            while True:
                try:
                    msg = snapshot_q.get_nowait()
                    self._last_snapshot = msg
                    cvd = msg.get("cvd", {})
                    session_ctx = msg.get("session_context", {})
                    weekly_ctx = msg.get("weekly_context", {})
                    shape_ctx = msg.get("profile_shape", {})
                    composite_ctx = msg.get("composite_context", {})
                    divergence = msg.get("divergence")

                    snap_row = {
                        "timestamp":        msg.get("timestamp"),
                        "mid_price":        msg.get("mid_price"),
                        "spread":           msg.get("spread"),
                        "imbalance_5":      msg.get("imbalance_5"),
                        "imbalance_20":     msg.get("imbalance_20"),
                        "best_bid":         msg.get("best_bid"),
                        "best_ask":         msg.get("best_ask"),
                        "rolling_delta":    cvd.get("rolling_delta"),
                        "cumulative_delta": cvd.get("cumulative_delta"),
                        "cvd_ratio":        cvd.get("cvd_ratio"),
                        "trade_count":      cvd.get("trade_count"),
                        # Context Layer
                        "session":          session_ctx.get("session"),
                        "session_poc":      session_ctx.get("session_poc"),
                        "session_va_high":  session_ctx.get("session_value_area_high"),
                        "session_va_low":   session_ctx.get("session_value_area_low"),
                        "price_in_va":      session_ctx.get("price_in_value_area"),
                        "price_vs_poc":     session_ctx.get("price_vs_poc"),
                        "ib_high":          session_ctx.get("initial_balance_high"),
                        "ib_low":           session_ctx.get("initial_balance_low"),
                        "poc_drift_ratio":  session_ctx.get("poc_drift_ratio"),
                        "anomaly_factor":   (
                            session_ctx.get("pre_session_anomaly", {}).get("anomaly_factor")
                            if session_ctx.get("pre_session_anomaly") else None
                        ),
                        "week":             weekly_ctx.get("week"),
                        "week_poc":         weekly_ctx.get("week_poc"),
                        "week_va_high":     weekly_ctx.get("week_value_area_high"),
                        "week_va_low":      weekly_ctx.get("week_value_area_low"),
                        "profile_shape":    shape_ctx.get("shape"),
                        "composite_poc":    composite_ctx.get("composite_poc"),
                        "composite_balance": composite_ctx.get("balance_flag"),
                        "divergence_type":  divergence.get("divergence_type") if divergence else None,
                    }
                    self._snapshot_buf.append(snap_row)
                except asyncio.QueueEmpty:
                    break

            while True:
                try:
                    msg = signal_q.get_nowait()
                    self._log_signal(msg)
                except asyncio.QueueEmpty:
                    break

            now = asyncio.get_event_loop().time()
            if now - last_flush >= FLUSH_INTERVAL or len(self._trade_buf) >= BUFFER_MAX:
                self._flush_trades()
                self._flush_snapshots()
                last_flush = now

            await asyncio.sleep(0.5)

        self._flush_trades()
        self._flush_snapshots()
        logger.warning("Logger Agent gestoppt. Daten gesichert.")
