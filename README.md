# OrderFlow Pro

Multi-Exchange Order Flow Analysis — Live L2 Aggregation, CVD, Volume Profile, AI Signal Agent.

## Quick Start

```bash
cd C:\Users\ericl\OneDrive\MiroFish\orderflow-pro
python main.py
```

Browser öffnet automatisch auf http://localhost:8000

## Was läuft

- Live Binance L2 Order Book (100 Levels) + Trade Stream via ccxt.pro
- CVD (Cumulative Volume Delta) mit 200-Trade Rolling Window
- Volume Profile (VAP, $25 Buckets) aus historischen Kerzen
- DeepSeek Signal Agent alle 15s — BULLISH / BEARISH / NEUTRAL
- Daten-Logger: Trades + Snapshots -> Parquet, Signale -> JSONL

## Projekt Struktur

```
orderflow-pro/
├── main.py                        # Orchestrierung (alle Agents in asyncio)
├── agents/
│   ├── exchange_agent.py          # Binance WebSocket (L2 + Trades)
│   ├── aggregator_agent.py        # CVD, Delta, Imbalance
│   ├── signal_agent.py            # DeepSeek API Signal Agent
│   ├── display_agent.py           # FastAPI WebSocket -> Browser
│   └── logger_agent.py            # Parquet + JSONL Logger
├── core/
│   ├── orderbook.py               # L2 Book State + Metriken
│   ├── cvd.py                     # CVD Berechnung
│   ├── broker.py                  # asyncio.Queue Message Bus (lokal)
│   └── history.py                 # 60min Ring Buffer + VAP
├── scripts/
│   ├── download_history.py        # Binance data.binance.vision (30+ Tage)
│   └── replay_history.py          # Replay -> Trainings-Features
├── static/index.html              # Browser UI
├── .env                           # DEEPSEEK_API_KEY (nicht in git)
└── data/                          # Parquet + JSONL (nicht in git)
```

## Historische Daten

```bash
# Letzte 30 Tage herunterladen (~107M Trades)
python scripts/download_history.py --days 30

# Trainings-Features generieren (43k Samples)
python scripts/replay_history.py
```

Output: `data/training_features.parquet` — 43.195 Minuten-Samples mit +5/15/30min Labels.

## Message Bus

Lokal: `core/broker.py` — asyncio.Queue (kein Redis/Docker nötig)
Produktion (VPS): Redis Pub/Sub — einfacher Swap, keine Agent-Code-Änderungen

Channels: `L2` · `TRADES` · `AGGREGATED` · `SIGNALS`

## Sprint Status

- Sprint 1: COMPLETE — Binance Exchange Agent
- Sprint 2: COMPLETE — Aggregator + CVD
- Sprint 3: COMPLETE — FastAPI + Lightweight Charts Frontend + Volume Profile
- Sprint 4: PENDING — Bybit + OKX Agents
- Sprint 5: COMPLETE — Signal Agent (DeepSeek API)
- Sprint 6: PENDING — ChromaDB Pattern Memory
- Sprint 7: PENDING — Docker + VPS Deploy (Redis Migration)
