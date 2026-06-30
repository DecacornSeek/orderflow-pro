# OrderFlow Pro — Master Context

## Produkt These
Trader haben TradingView offen aber sehen nur den Preis. 
OrderFlow Pro ist der zweite Bildschirm daneben: Echtzeit 
Order Flow aus 3 Exchanges + KI-Kontext der erklärt was passiert.

## Zielgruppe
Phase 1: Eigener Gebrauch (Crypto Intraday, Perps + Spot)
Phase 2: Alpha Gruppe 5-10 Trader (€49-99/Monat)
Phase 3: Public Launch + Prop Firm Tier

## Tech Stack
- Data:        ccxt pro (unified WebSocket für alle Exchanges)
- Async:       Python asyncio (parallele Agent-Loops)
- Message Bus: Redis Pub/Sub (Agents kommunizieren nur über Redis)
- Agents:      OpenAI Swarm (Orchestrierung)
- Intelligence: Claude API (Signal Agent + Market Narrator)
- Memory:      ChromaDB (Pattern Memory / RAG)
- Backend:     FastAPI + WebSocket Server
- Frontend:    Next.js + TradingView Lightweight Charts + D3.js
- Infra:       Docker + Hetzner VPS

## Projekt Struktur
orderflow-pro/
├── agents/
│   ├── exchange_agent.py      # Ein Agent pro Exchange
│   ├── aggregator_agent.py    # CVD, Delta, Imbalance
│   ├── signal_agent.py        # Claude-powered Analysis
│   └── display_agent.py       # WebSocket → Frontend
├── core/
│   ├── orderbook.py           # L2 Book State Management
│   ├── cvd.py                 # CVD Berechnung
│   └── liquidations.py        # Liquidation Event Processing
├── frontend/
│   ├── pages/
│   └── components/
│       ├── Heatmap.jsx
│       ├── CVDChart.jsx
│       └── SignalPanel.jsx
├── infra/
│   ├── docker-compose.yml
│   └── redis.conf
├── CLAUDE.md
└── requirements.txt

## Agent Architektur
Jeder Agent läuft als kontinuierlicher asyncio Loop:
- Exchange Agents: WebSocket offen, pushen zu Redis
- Aggregator: subscribed auf alle Exchange Channels, berechnet
- Signal Agent: analysiert aggregierte Daten, Claude API Call
- Display Agent: pushed via WebSocket ans Frontend

Redis Channels:
- binance_l2, bybit_l2, okx_l2       → raw L2 data
- binance_trades, bybit_trades, ...   → trade stream
- binance_liquidations, ...           → liquidation events
- aggregated_cvd                      → multi-exchange CVD
- signals                             → KI signals + narrator

## L2 Order Book Requirements
- Top 100 Bids + Top 100 Asks pro Exchange
- Snapshot beim Start, dann Delta Updates
- Sequence Number Validierung — bei Gap: resync
- Size = 0 → Level löschen
- Exponential backoff reconnect (1s, 2s, 4s, 8s, max 60s)
- Berechne: spread, mid_price, imbalance (top 5 + top 20)

## Redis Output Format (alle Exchange Agents)
{
  "exchange": "binance",
  "symbol": "BTCUSDT",
  "timestamp": <unix ms>,
  "bids": [[price, size], ...],   # top 100
  "asks": [[price, size], ...],   # top 100
  "imbalance_5":  <float 0-1>,    # top 5 levels
  "imbalance_20": <float 0-1>,    # top 20 levels
  "spread": <float>,
  "mid_price": <float>,
  "last_update_id": <int>
}

## Trade Stream Format
{
  "exchange": "binance",
  "symbol": "BTCUSDT",
  "timestamp": <unix ms>,
  "price": <float>,
  "size": <float>,
  "aggressor_side": "buy" | "sell",
  "trade_id": <string>
}

## Funktionale Features (Priorität)
CORE (Sprint 1-4):
- Live Heatmap (L2 Order Book als Farbkodierung)
- Multi-Exchange CVD aggregiert + per Exchange
- Echte Liquidation Bubbles (nicht geschätzt)

SIGNAL (Sprint 5-6):
- Spoofing Detection
- Absorption Erkennung  
- Funding Rate + OI + CVD Kombination

ALPHA (Sprint 7+):
- Claude Market Narrator (Echtzeit Kontext-Satz)
- Pattern Memory via ChromaDB (RAG auf historischem Order Flow)

## Nicht-funktionale Requirements
- Latenz: Ende-zu-Ende unter 50ms
- Uptime: 24/7, Auto-Reconnect, kein manuelles Restart
- Datentreue: Sequence Validation, kein corrupted Book
- UX: Browser-Fenster neben TradingView, Dark Mode
- Privacy: Nur public market data, keine API Keys nötig

## Coding Prinzipien
- Immer asyncio, NIE blocking code
- Jeder Agent läuft unabhängig
- Redis ist der EINZIGE Kommunikationskanal zwischen Agents
- Fehler loggen, nie crashen — reconnect logic immer dabei
- Kommentare auf Deutsch
- Nach jedem Sprint: CLAUDE.md updaten mit Decisions + Status

## Architektur-Entscheidung: Broker (asyncio.Queue statt Redis lokal)
- Lokal (Windows, kein Docker/WSL): core/broker.py — asyncio.Queue als Message Bus
- Produktion (Hetzner VPS): Redis Pub/Sub wie urspruenglich geplant
- Die Agent-Interfaces sind identisch — nur broker.publish/subscribe vs redis.publish/subscribe
- Migration zu Redis: broker.py durch redis-adapter ersetzen, kein Agent-Code aendern

## Tech Stack (aktuell)
- Data:        ccxt pro (Binance WebSocket)
- Async:       Python asyncio
- Message Bus: core/broker.py (asyncio.Queue) lokal / Redis Pub/Sub auf VPS
- Intelligence: DeepSeek API (signal_agent.py) — kompatibel mit OpenAI SDK
- Backend:     FastAPI + WebSocket (agents/display_agent.py)
- Frontend:    static/index.html — TradingView Lightweight Charts + Volume Profile
- Data Store:  Parquet (data/) + JSONL fuer Signale

## Status Update (2026-06-30)
Sprint 3 abgeschlossen:
  - agents/display_agent.py — FastAPI WebSocket Server + static file serving
  - static/index.html — Live Chart (Kerzen + CVD + Volume Profile) + Signal Log
  - core/history.py — 60-Minuten Ring Buffer, VAP, historische Kerzen von Binance REST
  - main.py — Orchestrierung aller Agents, Browser oeffnet automatisch

Sprint 5 (Signal Agent) abgeschlossen:
  - agents/signal_agent.py — DeepSeek API, alle 15s, Marktkontext-Analyse

Daten-Pipeline abgeschlossen:
  - agents/logger_agent.py — Trades + Snapshots -> Parquet, Signale -> JSONL
  - scripts/download_history.py — Binance data.binance.vision, 30 Tage = 107M Trades
  - scripts/replay_history.py — Replay -> 43.195 Trainings-Samples mit Labels (+5/15/30min)

## Architektur-Haertung (PR1.x + PR4a) — COMPLETE 2026-06-30

### PR1.1 — Runtime Contract Enforcement (TRADES + AGGREGATED)
  - core/validators.py — validate_trade_event(), validate_l2_snapshot(),
    validate_aggregated_snapshot() — alle geben (bool, str) zurueck
  - core/metrics.py — in-process Counter Dict; increment/get/snapshot/reset_all;
    benannte Konstanten fuer alle Keys
  - agents/exchange_agent.py — validate vor jedem broker.publish (TRADES + L2);
    skip + increment bei Fehler
  - agents/aggregator_agent.py — validate vor AGGREGATED publish; skip + increment
  - agents/display_agent.py — GET /metrics Endpoint (alle Counter als JSON)
  - test_pr1_1.py — 23 Tests; Validators unit, Counter unit, Integration loops,
    /metrics Endpoint

### PR4a — Context Layer: Session + Weekly Profiles
  - core/session_profile.py: ingest_trade(), snapshot(), reset_if_needed(),
    _compute_regime() — neu; "regime" Feld in current_context() Output
  - core/weekly_profile.py: identisches Interface; _week_start_ms() Anker
    Sonntag 22:00 UTC; _reset_week() Helper fuer DRY Reset
  - agents/aggregator_agent.py: per-Modul try/except fuer session + weekly;
    _throttled_warn() (1 Log pro Key pro 60s, Counter zaehlt immer);
    neue Counter: context_session_fail_total, context_weekly_fail_total,
    context_fallback_total
  - tests/__init__.py, tests/test_session_profile.py (37 Asserts),
    tests/test_weekly_profile.py (30 Asserts),
    tests/test_boundaries.py (38 Asserts — UTC midnight + Sonntag 22:00)

### PR4a Verification Scripts
  - scripts/verify_pr4a_contract.py — Payload vor/nach, Legacy-Felder 15/15,
    nur additive "regime" Keys; PASS
  - scripts/verify_pr4a_isolation.py — session exception Zyklen 1-3, weekly
    immer gueltig, Throttle 3 Fehler -> 1 Log; PASS (9/9)
  - scripts/verify_pr4a_perf.py — 10k Trades in 233ms (42k trades/s),
    Publish p99=2.5ms, Budget-Margin 333x bei 1Hz; PASS

### Regime Heuristik (session + weekly)
  - Bucket-Vergleich: _bucket(last_price) > va_h -> imbalanced_up
  - _bucket(last_price) < va_l -> imbalanced_down
  - innerhalb VA und Breite >= 50% price_range -> balanced
  - sonst -> imbalanced; keine Daten -> neutral

### Naechster Schritt: PR4b (Bybit + OKX Agents)

## Sprint Status
Sprint 1: COMPLETE — Binance Exchange Agent
Sprint 2: COMPLETE — Aggregator + CVD
Sprint 3: COMPLETE — FastAPI + Lightweight Charts Frontend + Volume Profile
Sprint 4: PENDING — Bybit + OKX Agents
Sprint 5: COMPLETE — Signal Agent (DeepSeek API)
Sprint 6: PENDING — ChromaDB Pattern Memory
Sprint 7: PENDING — Docker + VPS Deploy (Redis Migration)
PR1.x:   COMPLETE — Contract Validation + Metrics + /metrics Endpoint
PR4a:    COMPLETE — Session/Weekly Context Layer + Regime + 105 Tests verified

## Daten-Struktur
data/
  historical/trades_YYYY-MM-DD.parquet  # Binance historische Trades (107M+)
  trades_YYYY-MM-DD.parquet             # Live-Trades vom Logger
  snapshots_YYYY-MM-DD.parquet          # CVD + L2 Snapshots (1/s)
  signals_YYYY-MM-DD.jsonl              # Signale + Kontext (LLM Training)
  training_features.parquet             # 43k Minuten-Features mit Preis-Labels

┌─────────────────────────────────────────────────┐
│              ORCHESTRATOR                        │
└──┬──────────┬──────────┬───────────┬────────────┘
   │          │          │           │
Exchange   Exchange   Exchange   Funding/OI
Agents     Agents     Agents     Agent
(L2+Trades (L2+Trades (L2+Trades (alle Exchanges)
+Liq.)     +Liq.)     +Liq.)
   └──────────┼──────────┘           │
              │                      │
      ┌───────▼──────────────────────▼───┐
      │        AGGREGATOR AGENT          │
      │  Multi-Exchange CVD, Delta,      │
      │  Divergenz, Liquidation Cluster  │
      └───────────────┬──────────────────┘
                      │
      ┌───────────────▼──────────────────┐
      │         SIGNAL AGENT             │
      │  Spoofing, Absorption, Squeeze   │
      │  Setup Detection                 │
      └──────┬──────────────┬────────────┘
             │              │
      ┌──────▼──────┐  ┌────▼────────────┐
      │  RAG/Memory │  │  LLM Narrator   │
      │  (Vektor DB)│  │  (Market Story) │
      └──────┬──────┘  └────┬────────────┘
             └──────┬───────┘
                    │
      ┌─────────────▼────────────────────┐
      │         DISPLAY AGENT            │
      │  Heatmap + CVD + Liquidations    │
      │  + Narrator Text + Alerts        │
      └──────────────────────────────────┘