"""
Logger Agent — speichert Live-Daten für späteres Training.

Schreibt:
  data/trades_YYYY-MM-DD.parquet      — rohe Trades (Preis, Size, Side)
  data/snapshots_YYYY-MM-DD.parquet   — CVD + L2 Metriken (1/s)
  data/signals_YYYY-MM-DD.jsonl       — Signal + Marktkontext (LLM Training)

Jede Signal-Zeile in JSONL:
  {timestamp, context: {mid_price, imbalance_5, imbalance_20, rolling_delta,
   cumulative_delta, cvd_ratio, bids_top5, asks_top5}, signal, price_at_signal,
   price_5min_later: null, price_15min_later: null}

price_X_later wird nachträglich per scripts/label_signals.py befüllt.
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
FLUSH_INTERVAL = 60   # Sekunden zwischen Parquet-Flushes
BUFFER_MAX     = 1000  # erzwungener Flush bei N Trades


class LoggerAgent:

    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self._trade_buf:    List[dict] = []
        self._snapshot_buf: List[dict] = []
        self._last_snapshot: dict = {}   # aktueller Marktkontext für Signal-Logs
        DATA_DIR.mkdir(exist_ok=True)

    # ── Hilfsmethoden ────────────────────────────────────────────────────────

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

    # ── Flush Trades ──────────────────────────────────────────────────────────

    def _flush_trades(self) -> None:
        if not self._trade_buf:
            return
        path = DATA_DIR / f"trades_{self._today()}.parquet"
        self._parquet_append(path, self._trade_buf)
        logger.debug("Trades gespeichert: %d → %s", len(self._trade_buf), path.name)
        self._trade_buf.clear()

    # ── Flush Snapshots ───────────────────────────────────────────────────────

    def _flush_snapshots(self) -> None:
        if not self._snapshot_buf:
            return
        path = DATA_DIR / f"snapshots_{self._today()}.parquet"
        self._parquet_append(path, self._snapshot_buf)
        logger.debug("Snapshots gespeichert: %d → %s", len(self._snapshot_buf), path.name)
        self._snapshot_buf.clear()

    # ── Signal als JSONL speichern ────────────────────────────────────────────

    def _log_signal(self, signal_msg: dict) -> None:
        snap = self._last_snapshot
        cvd  = snap.get("cvd", {})

        record = {
            "timestamp":      signal_msg.get("timestamp"),
            "signal":         signal_msg.get("text", ""),
            "level":          signal_msg.get("level", "info"),
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
            },
            # Nachträglich befüllen mit scripts/label_signals.py
            "price_5min_later":  None,
            "price_15min_later": None,
            "price_30min_later": None,
        }

        path = DATA_DIR / f"signals_{self._today()}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        ts = self._ts_to_hms(record["timestamp"])
        logger.warning("Signal geloggt [%s]: %s", ts, record["signal"][:60])

    # ── Haupt-Loop ────────────────────────────────────────────────────────────

    async def run(self, shutdown: asyncio.Event) -> None:
        trade_q    = self.broker.subscribe(TRADES)
        snapshot_q = self.broker.subscribe(AGGREGATED)
        signal_q   = self.broker.subscribe(SIGNALS)

        last_flush = asyncio.get_event_loop().time()
        logger.warning("Logger Agent gestartet → %s", DATA_DIR)

        while not shutdown.is_set():

            # --- Trades buffern ---
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

            # --- Snapshots buffern + letzten Kontext halten ---
            while True:
                try:
                    msg = snapshot_q.get_nowait()
                    self._last_snapshot = msg
                    cvd = msg.get("cvd", {})
                    self._snapshot_buf.append({
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
                    })
                except asyncio.QueueEmpty:
                    break

            # --- Signale sofort als JSONL schreiben ---
            while True:
                try:
                    msg = signal_q.get_nowait()
                    self._log_signal(msg)
                except asyncio.QueueEmpty:
                    break

            # --- Flush wenn Zeit oder Buffer voll ---
            now = asyncio.get_event_loop().time()
            if now - last_flush >= FLUSH_INTERVAL or len(self._trade_buf) >= BUFFER_MAX:
                self._flush_trades()
                self._flush_snapshots()
                last_flush = now

            await asyncio.sleep(0.5)

        # Finaler Flush beim Shutdown
        self._flush_trades()
        self._flush_snapshots()
        logger.warning("Logger Agent gestoppt. Daten gesichert.")
