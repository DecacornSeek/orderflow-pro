# OrderFlow Pro

Multi-Exchange Order Flow Analysis — Live L2 Aggregation, CVD, Volume Profile, AI Signal Agent, Strategy Backtesting.

## Quick Start

```bash
cd C:\Users\ericl\OneDrive\MiroFish\orderflow-pro
python main.py                          # Live trading dashboard
python scripts/evaluate.py             # Verify backtesting stack (46 checks)
```

Browser öffnet automatisch auf http://localhost:8000

## Was läuft (Live Pipeline)

- Live Binance L2 Order Book (100 Levels) + Trade Stream via ccxt.pro
- CVD (Cumulative Volume Delta) mit 200-Trade Rolling Window
- Volume Profile (VAP, $25 Buckets) mit Session + Weekly Context
- Business Zones (POC/VAH/VAL/HVN/Single Prints) mit tested/repaired State Machine
- Daily Road Map (day_type, allowed setups, Point A→B Path)
- Lethargy Detector (3-dimensionale Volume/Range/Speed Decay Signatur)
- Multi-Week VPOC Trend Series (rising/falling/flattening mit Linear Regression)
- Inside Bar im Candle Classifier (NCI Priority 5)
- Delta Divergenz (Preis-Extreme ohne CVD-Bestaetigung)
- DeepSeek Signal Agent alle 15s mit Road Map + Zonen + VPOC Trend Kontext
- Daten-Logger: Trades + Snapshots -> Parquet, Signale -> JSONL

## Backtesting Stack (Sprint C)

```
strategies/
├── base.py          PropFirmRules (Breakout + FundingPips 1-step), RiskGuardrails,
│                    Position Sizing, BacktestResult, Equity Curve Simulator
└── backtest.py      vectorbt + Optuna Walk-Forward Engine
                     run_backtest() — signals → portfolio → prop firm verdicts
                     walk_forward_optimize() — Optuna TPE per fold
```

- **vectorbt** (Portfolio.from_signals) — vectorized portfolio simulation
- **Optuna** (TPESampler) — Bayesian hyperparameter optimization
- **180 Tage** Binance trade data (Jan–Jul 2026, 629M trades, 2 GB parquet)
- Verifiziert: `python scripts/evaluate.py` — 46/46 checks pass

## Projekt Struktur

```
orderflow-pro/
├── main.py                        # Orchestrierung (alle Agents in asyncio)
├── agents/
│   ├── exchange_agent.py          # Binance WebSocket (L2 + Trades)
│   ├── aggregator_agent.py        # CVD, Delta, Imbalance, Layer-3 Context
│   ├── signal_agent.py            # DeepSeek API Signal Agent (mit Road Map + Zonen)
│   ├── display_agent.py           # FastAPI + WebSocket Server
│   └── logger_agent.py            # Parquet + JSONL Logging
├── core/
│   ├── orderbook.py               # L2 Book State Management
│   ├── cvd.py                     # CVD Berechnung
│   ├── candle_classifier.py       # NCI Candle Classifier (Maru/Shock/Doji/Pin/Inside)
│   ├── session_profile.py         # Session VAP + Phasen + Initial Balance
│   ├── weekly_profile.py          # Weekly VAP (Sunday 22:00 UTC Reset)
│   ├── volume_profile.py          # BaseVolumeProfile (shared VAP/POC/VA/Regime)
│   ├── business_zones.py          # Zone Registry (POC/VAH/VAL/HVN/Single Prints)
│   ├── road_map.py                # Daily Road Map + Setup Matrix
│   ├── profile_structure.py       # Single Prints, Weak/Strong Extremes, Double Dist.
│   ├── profile_shape.py           # P/b/D/B Shape Classification
│   ├── profile_config.py          # Zentrale Schwellwerte (immutable frozen dataclass)
│   ├── absorption.py              # Absorption Detection
│   ├── pattern_engine.py          # Pattern Engine + Delta Divergence
│   ├── divergence.py              # CVD Divergence + Swing Tracking
│   ├── lethargy.py                # Lethargy Detector (Volume/Range/Speed Decay)
│   ├── vpoc_trend.py              # Multi-Week VPOC Trend (Linear Regression)
│   ├── breakout.py                # Breakout Tracker (NCI PAC)
│   ├── composite_profile.py       # Multi-Session HVN/LVN Composite
│   ├── broker.py                  # asyncio.Queue Message Bus
│   ├── validators.py              # Runtime Contract Enforcement
│   └── metrics.py                 # In-Process Counter Dict
├── strategies/                    # Sprint C — Strategy Engine
│   ├── base.py                    # PropFirmRules + Guardrails + BacktestResult
│   └── backtest.py                # vectorbt + Optuna Walk-Forward Engine
├── scripts/
│   ├── download_history.py        # Binance historical trade data downloader
│   ├── replay_history.py          # CVD feature replay (training_features.parquet)
│   └── evaluate.py                # Backtesting stack verification (46 checks)
├── tests/                         # 300+ tests (pytest)
├── data/
│   ├── historical/                # 180 Tage BTC trades (629M trades, 2 GB)
│   └── training_features.parquet  # 43K minute-level CVD features
├── docs/
│   └── METHODOLOGY_STEPS_5-8.md   # Kanonische Methodik-Referenz
├── infra/
│   ├── docker-compose.yml
│   └── Dockerfile
└── static/
    └── index.html                 # Live Dashboard
```

## Sprint Status

| Sprint | Status |
|---|---|
| Sprint 1-3 | ✅ Binance Agent + Aggregator + FastAPI Frontend |
| Sprint 5 | ✅ Signal Agent (DeepSeek API) |
| PR1.x + PR4a | ✅ Contract Validation + Session/Weekly Context |
| Sprint B | ✅ Delta Divergenz + Session-Phasen + Profil-Konsolidierung |
| Sprint B2 | ✅ Interpretations-Layer (Steps 5-8): Zones, Road Map, Structure |
| Sprint B3 | ✅ Backlog: Lethargy, Inside Bar, VPOC Trend, Signal Prompt |
| Sprint C | 🚧 Strategy Engine (vectorbt + Optuna integration built) |
| Sprint D | ⏳ Optimizer + Hermes (Hetzner VPS) |

## Daten

- 180 Tage BTC/USDT Binance Spot Trades (Jan–Jul 2026)
- 629M Trades als Parquet (2 GB)
- 43K minute-level Training Features mit +5/15/30/60/240min Labels
- Quelle: https://data.binance.vision
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
