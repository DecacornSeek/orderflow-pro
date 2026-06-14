# OrderFlow Pro — Projekt Kontext

## Was wir bauen
Multi-Exchange Order Flow Analyse Tool mit:
- Live L2 Order Book Aggregation (Binance, Bybit, OKX)
- Echtzeit CVD, Delta, Imbalance Berechnung
- Heatmap Visualisierung (wie Bookmap)
- Echte Liquidation Events (nicht geschätzt)
- Claude-powered Signal Agent + Market Narrator
- Pattern Memory via ChromaDB (RAG)

## Tech Stack
- Backend: Python + asyncio + ccxt pro
- Message Bus: Redis Pub/Sub
- Agent Framework: OpenAI Swarm
- Intelligence: Claude API (Signal Agent)
- Memory: ChromaDB (Vektor DB)
- Frontend: Next.js + TradingView Lightweight Charts + D3.js
- Infra: Docker + Hetzner VPS

## Projekt Struktur
orderflow-pro/
├── agents/
│   ├── exchange_agent.py     # Ein Agent pro Exchange
│   ├── aggregator_agent.py   # CVD, Delta, Imbalance
│   ├── signal_agent.py       # Claude-powered Analysis
│   └── display_agent.py      # WebSocket → Frontend
├── core/
│   ├── orderbook.py          # L2 Book State Management
│   ├── cvd.py                # CVD Berechnung
│   └── liquidations.py       # Liquidation Event Processing
├── frontend/
│   ├── pages/
│   └── components/
│       ├── Heatmap.jsx
│       ├── CVDChart.jsx
│       └── SignalPanel.jsx
├── infra/
│   ├── docker-compose.yml
│   └── redis.conf
├── CLAUDE.md                 # Diese Datei
└── requirements.txt

## Build Reihenfolge (Sprints)
Sprint 1: Binance WebSocket → L2 Book + Trades (asyncio + ccxt pro)
Sprint 2: Redis + Aggregator → CVD live
Sprint 3: FastAPI + Lightweight Charts → erste Heatmap
Sprint 4: Bybit + OKX Agents
Sprint 5: Claude Signal Agent + Narrator
Sprint 6: ChromaDB Pattern Memory
Sprint 7: Docker + VPS Deploy

## Aktueller Sprint
Sprint 1 — Binance L2 Agent

## Coding Prinzipien
- Immer asyncio, kein blocking code
- Jeder Agent läuft unabhängig
- Redis als einziger Kommunikationskanal zwischen Agents
- Fehler werden geloggt, nie gecrasht (reconnect logic immer dabei)
- Kommentare auf english


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