# OrderFlow Pro

Multi-Exchange Order Flow Analysis — Live L2 Aggregation, CVD, Heatmap, AI Signal Agent.

## Quick Start (3 Befehle)

```bash
# 1. Redis starten
docker compose -f infra/docker-compose.yml up redis -d

# 2. Dependencies installieren & Agent starten
pip install -r requirements.txt
python -m agents.exchange_agent

# 3. Redis Output live prüfen
python test_sprint1.py
```

## Agent testen

### Option A — Live Redis Output beobachten
```bash
# Redis CLI (braucht Redis lokal oder via Docker)
redis-cli SUBSCRIBE binance_l2 binance_trades
```

### Option B — Smoke Test (kein Redis nötig)
```bash
python test_sprint1.py
```
Prüft: WebSocket Connect, L2 Book (100 levels), Trade Stream, Aggressor Side, Metriken.

### Option C — Book State validieren
```bash
python -c "
import asyncio, json
import redis.asyncio as redis

async def watch():
    r = redis.from_url('redis://localhost:6379')
    ps = r.pubsub()
    await ps.subscribe('binance_l2', 'binance_trades')
    count = 0
    async for msg in ps.listen():
        if msg['type'] != 'message': continue
        data = json.loads(msg['data'])
        ch = msg['channel']
        if ch == 'binance_l2':
            print(f\"L2  mid={data['mid_price']:.2f}  spread={data['spread']:.4f}  imb5={data['imbalance_5']:.3f}  levels={len(data['bids'])}/{len(data['asks'])}\")
        else:
            print(f\"TRD price={data['price']}  side={data['aggressor_side']}  size={data['size']}\")
        count += 1
        if count >= 20: break
    await r.aclose()

asyncio.run(watch())
"
```

## Projekt Struktur
```
orderflow-pro/
├── agents/exchange_agent.py   # Binance WebSocket Agent (Sprint 1)
├── core/orderbook.py          # L2 Book State + Metriken
├── infra/
│   ├── docker-compose.yml     # Redis + Agent Container
│   ├── Dockerfile             # Agent Image
│   └── redis.conf             # Redis Konfiguration
├── requirements.txt
└── CLAUDE.md                  # Projekt Kontext
```

## Redis Channels
| Channel | Inhalt |
|---|---|
| `binance_l2` | L2 Order Book Snapshot (top 100, + spread/imbalance) |
| `binance_trades` | Trade Stream mit aggressor_side |

## Sprint Status
- ✅ Sprint 1: Binance WebSocket Agent
- ⏳ Sprint 2: Aggregator + CVD
- ⏳ Sprint 3: FastAPI + Heatmap Frontend
