# OrderFlow Pro — System Architecture
**Version:** 2.0 | **Perspective:** System Engineer
**Scope:** BTC/USDT Perps, Intraday 1min–4H, Discretionary + Autonomous Optimization

---

## 1. System Overview

```
EXTERNAL DATA SOURCES
┌─────────────┬──────────────┬────────────────┬───────────┬────────────┐
│Binance Perps│ Binance Spot │  Bybit Perps   │ OKX Perps │  Coinbase  │
│L2+Trades+FR │  L2+Trades   │ L2+Trades+Liq  │L2+Trades  │   Spot     │
│    +OI+Liq  │              │    +FR+OI      │  +Liq     │  L2+Trades │
└──────┬──────┴──────┬───────┴───────┬────────┴─────┬─────┴─────┬──────┘
       │             │               │              │           │
       └─────────────┴───────────────┴──────────────┴───────────┘
                                     │
                            REDIS PUB/SUB (VPS)
                         asyncio.Queue (local dev)
                                     │
         ┌───────────────────────────┼──────────────────────────┐
         │                           │                          │
   ┌─────▼──────┐            ┌──────▼──────┐           ┌───────▼──────┐
   │  FEATURE   │            │  STRATEGY   │           │   CONTEXT    │
   │   ENGINE   │            │   ENGINE    │           │    ENGINE    │
   │            │            │             │           │              │
   │ CVD/Spot   │            │ NCI (Python)│           │ Session Prof │
   │ CVD/Perps  │            │ CCT (Python)│           │ Weekly Prof  │
   │ Divergence │            │ Composite   │           │ CME Gap      │
   │ Coinbase Δ │            │ Strategies  │           │ Funding+OI   │
   │ Z-Score    │            │             │           │ Macro Cal    │
   │ Absorption │            └──────┬──────┘           └───────┬──────┘
   └─────┬──────┘                   │                          │
         │                          └──────────┬───────────────┘
         │                                     │
         │                          ┌──────────▼──────────┐
         │                          │  STRATEGY OPTIMIZER  │
         │                          │   (Hermes / VPS)     │
         │                          │                      │
         │                          │ Prop Firm Objective  │
         │                          │ Walk-Forward Valid.  │
         │                          │ Parameter Sweep      │
         │                          │ PASS/FAIL Verdict    │
         │                          └──────────┬──────────┘
         │                                     │
         └─────────────────────┬───────────────┘
                               │
                    ┌──────────▼──────────┐
                    │    SIGNAL AGENT      │
                    │   (DeepSeek API)     │
                    │                      │
                    │ Interpretiert nur    │
                    │ bereits berechnete   │
                    │ deterministische     │
                    │ Signale — kein       │
                    │ eigenes Urteil       │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼─────────────────┐
              │                │                 │
    ┌─────────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
    │   CO-PILOT     │  │  DASHBOARD  │  │   REPORT    │
    │  (Trade Mgmt)  │  │  (FastAPI)  │  │  (Weekly)   │
    └────────────────┘  └─────────────┘  └─────────────┘
```

---

## 2. Architektur-Prinzipien

**P1 — CVD Spot vs. Perps niemals aggregieren.**
Divergenz zwischen Spot-CVD und Perps-CVD ist das Signal. Aggregat
vernichtet diese Information. Jede Exchange, jeder Typ bekommt einen
eigenen CVD-Stream.

**P2 — Deterministik vor LLM.**
Kein LLM-Urteil über Signifikanz, Edge oder Trade-Qualität.
LLM (DeepSeek) interpretiert nur was der Feature Engine bereits
deterministisch berechnet hat. PASS/FAIL kommt vom Optimizer, nicht
vom Modell.

**P3 — Jede Strategie ist eine reine Funktion.**
```python
def strategy(features: pd.DataFrame, params: dict) -> pd.Series:
    # +1 Long / -1 Short / 0 Neutral
    # Kein State, keine Side-Effects
```
Ermöglicht paralleles Backtesting ohne Konflikte.

**P4 — Broker Interface ist stabil.**
Lokal: `core/broker.py` (asyncio.Queue)
Produktion: Redis Pub/Sub
Agents sprechen nur über den Broker. Kein direkter Agent-zu-Agent Call.

**P5 — CME ist Kontext, kein Realtime-Feed.**
CME Gap Level (Freitag Close vs. Sonntag Open) einmal wöchentlich
als statischer Kontext-Input. Kein kostenpflichtiger Realtime-Feed.

---

## 3. Data Pipeline — Vollständige Spezifikation

### 3.1 Exchange Agents (Layer 1)

| Agent | Exchange | Typ | Daten | Status |
|-------|----------|-----|-------|--------|
| `exchange_agent_binance_perps.py` | Binance Futures | Perps | L2 + Trades + Funding + OI + Liq | ⚠️ Partial (L2+Trades only) |
| `exchange_agent_binance_spot.py` | Binance | Spot | L2 + Trades | ❌ Missing |
| `exchange_agent_bybit_perps.py` | Bybit | Perps | L2 + Trades + Funding + OI + Liq | ❌ Missing |
| `exchange_agent_okx_perps.py` | OKX | Perps | L2 + Trades + Liq | ❌ Missing |
| `exchange_agent_coinbase_spot.py` | Coinbase | Spot | L2 + Trades | ❌ Missing |

**Warum genau diese fünf:**
- Binance Perps: ~30% BTC Perps Volumen, Referenz-Exchange
- Binance Spot: Basis für Coinbase Premium Berechnung
- Bybit Perps: ~25% Volumen, bessere Liquidations-API als Binance
- OKX Perps: ~20% Volumen, unterschiedliche Trader-Base
- Coinbase Spot: institutionelles US-Venue, Coinbase Premium Index

**Nicht in Scope (V1):**
- Kraken, Gate, Huobi → marginales Volumen, kein Mehrwert
- CME Realtime → kostenpflichtig, US-Marktzeiten only
- Deribit Options → Phase 2, nach validiertem Volume Edge

### 3.2 Channel-Kontrakte (Broker)

```
TRADES_BINANCE_PERPS    → {exchange, type, ts, price, size, side}
TRADES_BINANCE_SPOT     → {exchange, type, ts, price, size, side}
TRADES_BYBIT_PERPS      → {exchange, type, ts, price, size, side}
TRADES_OKX_PERPS        → {exchange, type, ts, price, size, side}
TRADES_COINBASE_SPOT    → {exchange, type, ts, price, size, side}

L2_BINANCE_PERPS        → {exchange, type, ts, bids, asks,
                            spread, mid_price, imbalance_5, imbalance_20}
L2_BINANCE_SPOT         → (gleich)
L2_BYBIT_PERPS          → (gleich)
L2_OKX_PERPS            → (gleich)
L2_COINBASE_SPOT        → (gleich)

FUNDING                 → {exchange, ts, funding_rate, next_funding_ts,
                            open_interest, oi_change_4h}
LIQUIDATIONS            → {exchange, type, ts, price, size, side}

AGGREGATED              → {ts, mid_price, spread,
                            cvd_binance_perps, cvd_binance_spot,
                            cvd_bybit_perps, cvd_okx_perps,
                            cvd_coinbase_spot,
                            cvd_perps_total, cvd_spot_total,
                            spot_perps_divergence,
                            coinbase_premium,
                            funding_rate, open_interest,
                            imbalance_5, imbalance_20,
                            liq_cluster_long, liq_cluster_short}

PATTERNS                → {ts, price_bucket, z_score, volume,
                            absorption, exchange_source}
SIGNALS                 → {ts, text, level, setup_score, params}
CONTEXT                 → {ts, session_phase, weekly_vap_position,
                            daily_poc, cme_gap_level,
                            is_news_window, htf_trend}
```

### 3.3 Coinbase Premium Index

```python
# Berechnung im Aggregator Agent
coinbase_premium = coinbase_mid_price - binance_spot_mid_price

# Interpretation:
# > +15$  : US-institutionelle Nachfrage, bullish
# < -15$  : Retail/asiatischer Verkaufsdruck, bearish
# Absoluter Level weniger wichtig als Richtung und Momentum
```

### 3.4 Funding Rate + Open Interest

```python
# Binance Futures REST (kostenlos, ccxt verfügbar)
# Polling Interval: alle 30s reicht (Funding wird stündlich berechnet)

# Relevante Kombinationen:
# Funding sehr positiv (>0.05%) + OI steigt + CVD Perps > CVD Spot
# → Leveraged Long Squeeze Setup: Long-Positionen gezwungen zu schließen

# Funding sehr negativ (<-0.05%) + OI steigt + CVD Perps < CVD Spot
# → Short Squeeze Setup
```

### 3.5 CME Gap (wöchentlicher Kontext)

```python
# Kein Realtime-Feed nötig.
# Jeden Montag 00:00 UTC einmalig berechnen:
# cme_gap_level = freitag_close_price - sonntag_open_price
# cme_gap_open = abs(cme_gap_level) > 100  # $100 Mindestgröße

# Wird als statischer Wert in den Context Engine geschrieben.
# Gilt für die gesamte Handelswoche als Referenzlevel.
```

---

## 4. Feature Engine (Layer 2)

### 4.1 CVD Features — Kern des Systems

```python
class CVDFeatures:
    # Pro Exchange, pro Typ — SEPARATE Instanzen
    cvd_binance_perps:  CVD(window=200)
    cvd_binance_spot:   CVD(window=200)
    cvd_bybit_perps:    CVD(window=200)
    cvd_okx_perps:      CVD(window=200)
    cvd_coinbase_spot:  CVD(window=200)

    # Aggregierte Werte (berechnet, nicht direkt gestreamt)
    cvd_perps_total:    sum(perps CVDs)
    cvd_spot_total:     sum(spot CVDs)

    # Das eigentliche Signal
    spot_perps_divergence: cvd_perps_total - cvd_spot_total
    # Positiv = Perps führen Spot = spekulativer Druck
    # Negativ = Spot führt Perps = echte Nachfrage
```

### 4.2 Pattern Engine Features

```python
# Bereits implementiert in core/pattern_engine.py
# Z-Score Significance: volumetrische Anomalie vs. historische Baseline
# Absorption Detection: |delta|/volume Ratio
# Session VAP: Volume at Price pro Session

# FEHLT NOCH (kritisch):
# Delta Divergenz: Preis macht neues High, CVD nicht
# → Stärkstes deterministisches Reversal-Signal
```

### 4.3 Session & Market Structure Features

```python
# Module: core/session_profile.py (Sprint pending)

class SessionProfile:
    # Benannte Zustände — keine impliziten Lücken
    PHASES = [
        "asia",           # 00:00–08:00 UTC
        "london_pre",     # 07:00–08:00 UTC
        "london_open",    # 08:00–10:00 UTC
        "london_session", # 10:00–12:00 UTC
        "ny_pre",         # 12:00–13:30 UTC
        "ny_open",        # 13:30–15:30 UTC
        "overlap",        # 13:30–16:00 UTC (London/NY)
        "ny_afternoon",   # 16:00–20:00 UTC
        "asia_pre",       # 20:00–00:00 UTC
    ]

    # Wöchentliches Volume Profile
    weekly_poc:     float  # Price Of Control der Woche
    weekly_vah:     float  # Value Area High (70% des Wochenvolumens)
    weekly_val:     float  # Value Area Low
    weekly_hvn:     list   # High Volume Nodes
    weekly_lvn:     list   # Low Volume Nodes (schnelle Preisbewegung erwartet)

    # Tages-Profile
    daily_poc:      float
    initial_balance_high:  float  # Erste Handelsstunde
    initial_balance_low:   float
```

### 4.4 Candle Pattern Features

```python
# Module: core/candle_classifier.py (neu)
# Input: OHLCV Kerze (1H, 4H, 1D)
# Output: Klassifikation + Stärke

PATTERNS = {
    "marubozu":     body_ratio > 0.85,  # Starker Trend
    "pin_bar":      wick_ratio > 0.65 and body_ratio < 0.30,  # Rejection
    "doji":         body_ratio < 0.05,  # Entscheidungslosigkeit
    "engulfing":    prev_body < curr_body and opposite_color,  # Reversal
    "inside_bar":   high < prev_high and low > prev_low,  # Konsolidierung
    "outside_bar":  high > prev_high and low < prev_low,  # Expansion
    "normal":       else  # Keine besondere Klassifikation
}
```

---

## 5. Strategy Engine (Layer 3)

### 5.1 Dateistruktur

```
strategies/
├── __init__.py
├── base.py              # PropFirmConstraints Wrapper, BacktestResult
├── nci_strategy.py      # NCI Port (aus Pine Script)
├── cct_strategy.py      # CCT Port (aus Pine Script, SEPARATE)
├── composite/
│   ├── volume_trend.py  # Z-Score + HTF Trend
│   ├── session_vap.py   # Session Phase + VAP Position
│   └── squeeze.py       # Funding + OI + CVD Divergenz
└── backtest.py          # Walk-Forward Engine
```

### 5.2 Prop Firm Constraint Wrapper

```python
# Objective Function — deterministisch, kein LLM
@dataclass
class PropFirmRules:
    profit_target:       float = 0.10   # 10%
    max_daily_loss:      float = 0.05   # 5% Daily Drawdown
    max_total_drawdown:  float = 0.10   # 10% Total Drawdown
    min_trading_days:    int   = 10
    max_position_size:   float = 0.02   # 2% pro Trade
    # Challenge-spezifisch anpassen — FTMO, MFF, The5ers unterscheiden sich

@dataclass
class BacktestResult:
    profit_pct:          float
    max_drawdown:        float
    max_daily_loss:      float
    trading_days:        int
    win_rate:            float
    profit_factor:       float
    prop_firm_pass:      bool   # True/False — das ist die Zielmetrik
    equity_curve:        pd.Series
```

### 5.3 Delta Divergenz (fehlt, höchste Priorität)

```python
# core/pattern_engine.py Erweiterung
def detect_delta_divergence(
    price_highs: list,
    cvd_perps_highs: list,
    cvd_spot_highs: list,
) -> dict | None:
    """
    Bearish Divergenz: Preis neues High, CVD Perps kein neues High
    → Bullisher Druck nimmt ab trotz steigendem Preis
    → Setup: Short Entry wenn CVD Spot auch dreht

    Bullish Divergenz: Preis neues Low, CVD Perps kein neues Low
    → Sells trocknen aus, mögliche Reversal
    """
```

---

## 6. Strategy Optimizer (Layer 4)

### 6.1 Walk-Forward Engine

```python
# scripts/walk_forward_optimizer.py

def walk_forward(
    strategy_fn: Callable,
    param_grid: dict,
    features: pd.DataFrame,
    rules: PropFirmRules,
    train_weeks: int = 8,   # Baseline-Fenster
    test_weeks:  int = 1,   # Test-Fenster (rollt)
) -> pd.DataFrame:
    """
    1. Baseline aus Wochen [t-N, t)
    2. Strategie mit Params X auf Woche t testen
    3. PropFirmPass ja/nein
    4. Fenster + 1 Woche, wiederholen
    5. Aggregat-Verdict: pass_rate über alle Folds
    """

PASS_CRITERIA = {
    "min_pass_rate":     0.60,   # 60% der Folds positiv
    "min_profit_factor": 1.5,
    "max_drawdown":      0.08,   # 2pp Reserve unter Limit
}
```

### 6.2 Hermes auf Hetzner VPS

**Aufgabe:** Wöchentlicher autonomer Optimizer-Run

```
Trigger:  Jeden Sonntag 02:00 UTC (nach CME Weekend Gap Entstehung)
Input:    Neue Woche historischer Daten + aktuelle CME Gap
Output:   reports/strategy_report_YYYY-WW.md
Notify:   Telegram Nachricht mit PASS/FAIL Summary
```

**Hermes Task Definition:**
```
Ziel: Führe Walk-Forward Optimizer aus für alle Strategien in strategies/.
Objective Function: PropFirmRules (FTMO Standard).
Schreibe Ergebnis nach reports/.
Bei PASS: öffne Claude Code Session und schlage nächsten Build-Schritt vor.
Bei FAIL: erweitere Parameter Grid und wiederhole.
Schick mir Telegram wenn fertig.
```

---

## 7. Co-Pilot (Layer 5)

### 7.1 Trade State Machine

```python
# Discretionary Trader bleibt Entscheider
# Agent gibt Probabilistischen Input — kein Auto-Execute

TRADE_STATES = {
    "watching":    "Kein offener Trade, Setup-Monitoring aktiv",
    "setup":       "Setup erkannt, warte auf Entry-Trigger",
    "in_trade":    "Trade offen, Management-Monitoring aktiv",
    "at_breakeven":"SL auf BE, Trail-Management läuft",
    "scaling_out": "Partielle Position geschlossen",
    "closed":      "Trade beendet, PnL geloggt",
}

# Co-Pilot Output bei "in_trade":
{
    "recommendation": "break_even_now",
    "confidence": 0.71,
    "reason": "CVD Perps dreht, Spot CVD neutral — Perp-Druck lässt nach",
    "historical_basis": "In 71% ähnlicher Situationen: SL getroffen innerhalb 5min",
    "market_state": {
        "spot_perps_divergence": -0.42,
        "funding_rate": 0.0089,
        "session_phase": "ny_open",
    }
}
```

---

## 8. Deployment Topologie

```
LOKAL (Windows Dev Machine)
├── main.py                    Orchestrierung (asyncio)
├── core/broker.py             asyncio.Queue Message Bus
├── agents/
│   ├── exchange_agent_*.py    Binance Perps (live)
│   ├── aggregator_agent.py    CVD + Features
│   ├── signal_agent.py        DeepSeek API
│   └── display_agent.py       FastAPI + WebSocket
└── static/index.html          Dashboard (Browser)

HETZNER VPS (Produktion)
├── docker-compose.yml
│   ├── redis                  Message Bus (Pub/Sub)
│   ├── exchange-agents        5x Exchange Agents
│   ├── aggregator             CVD + Feature Engine
│   ├── signal-agent           DeepSeek API
│   ├── display-agent          FastAPI (Port 8000)
│   └── hermes                 Autonomer Optimizer Agent
├── data/
│   ├── historical/            Trade-History (Parquet)
│   ├── volume_baseline.parquet
│   └── strategy_reports/      Wöchentliche Reports
└── reports/
    └── strategy_report_*.md
```

---

## 9. Build-Sequenz

### Sprint A — Data Pipeline Completeness (jetzt)
**Ziel:** Alle 5 Exchange Agents live, CVD getrennt getracked
1. `exchange_agent_binance_spot.py` — Clone von Perps Agent
2. `exchange_agent_coinbase_spot.py` — Coinbase WebSocket via ccxt
3. `exchange_agent_bybit_perps.py` — Bybit + Funding + Liq
4. `exchange_agent_okx_perps.py` — OKX
5. Broker Channels erweitern (neue Channel-Namen)
6. Aggregator: CVD pro Stream + Coinbase Premium + Divergenz
7. **Test:** 5 CVD Streams laufen parallel, Divergenz berechnet

### Sprint B — Delta Divergenz + Session Profile
**Ziel:** Stärkstes deterministisches Signal implementieren
1. `detect_delta_divergence()` in `core/pattern_engine.py`
2. `core/session_profile.py` — Session-Phasen als benannte Zustände
3. Weekly VAP konsolidieren (aktuell 2 separate Instanzen)
4. VAP/POC/Value Area in Signal Agent Prompt (Fix aus Backlog)

### Sprint C — Strategy Engine
**Ziel:** NCI als backtestbare Python-Funktion
1. NCI Pine Script → `strategies/nci_strategy.py`
2. `strategies/base.py` — PropFirmConstraints Wrapper
3. `strategies/backtest.py` — Walk-Forward Engine
4. Erster Walk-Forward Run: NCI gegen 30 Tage History

### Sprint D — Optimizer + Hermes
**Ziel:** Autonomer wöchentlicher Optimizer-Loop
1. Optuna Integration für Parameter Sweep
2. Walk-Forward PASS/FAIL Report
3. Hermes auf Hetzner VPS aufsetzen
4. Telegram Notification
5. Wöchentlicher Automation Schedule

### Sprint E — Co-Pilot
**Ziel:** Live Trade Management Assistance
1. Trade State Machine
2. Similarity Search auf historischen Trade-States
3. Dashboard Co-Pilot Panel

---

## 10. Was nicht gebaut wird (V1)

| Feature | Begründung |
|---------|------------|
| CME Realtime-Feed | Kostenpflichtig, US-Marktzeiten only, kein Intraday-Edge |
| Deribit Options / GEX | Phase 2 — erst nach validiertem Volume Edge |
| On-Chain Daten (Glassnode etc.) | Tagesauflösung, retroaktiv revidiert, zu langsam |
| Coinglass Heatmap API | $699/Monat — Bybit/OKX raw Liq-Streams als Alternative |
| ETH / SOL Port | Nach stabiler BTC Pipeline |
| Auto-Execute Trading | Trader bleibt Entscheider — Co-Pilot only |
| TradingView Live-Integration | Phase 2 — CSV Export reicht für Backtesting |
