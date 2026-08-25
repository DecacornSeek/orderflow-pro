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

## Sprint B (Architektur V2) — COMPLETE 2026-07-02

### 1. Delta Divergenz (staerkstes deterministisches Signal, Spec §5.3)
  - core/pattern_engine.py: detect_delta_divergence() — reine Funktion (P3),
    vergleicht letzte zwei bestaetigte Swings; bearish = Preis HH + CVD Perps
    kein neues High, bullish = Preis LL + CVD kein neues Low; strength 0..1
    normiert; spot_confirms True/False/None (None bis Sprint A Spot-Streams)
  - core/divergence.py: DivergenceDetector fuehrt jetzt Swing-Serien
    (_swing_highs/_swing_lows, maxlen=20) + get_swing_highs()/get_swing_lows()
  - agents/aggregator_agent.py: "delta_divergence" Feld im AGGREGATED Payload,
    per try/except isoliert (Throttled Warn Key "delta_div")
  - tests/test_delta_divergence.py — 30 Asserts

### 2. Session-Phasen + Initial Balance (Spec §4.3)
  - core/session_profile.py: PHASE_DEFS — 9 benannte Phasen, Minuten-
    Aufloesung (asia, london_pre, london_open, london_session, ny_pre,
    ny_open 13:30-15:30, overlap 15:30-16:00, ny_afternoon, asia_pre);
    lueckenlos ueber 24h; phase_name_for_minute() / phase_for_ts()
  - Spec-Konflikt aufgeloest: ny_open gewinnt im London/NY-Ueberlapp,
    overlap deckt Rest 15:30-16:00
  - "session_phase" im session_context — aus letztem Trade-ts (replay-sicher)
  - Initial Balance jetzt auch WAEHREND der Bildung sichtbar +
    "initial_balance_complete" Flag (vorher erst nach 60min publiziert)
  - tests/test_session_phases.py — 35 Asserts

### 3. Profil-Konsolidierung (Weekly VAP 2 Instanzen -> 1)
  - core/volume_profile.py NEU: BaseVolumeProfile + ProfileSnapshot +
    _bucket/_compute_ohlc/_compute_poc/_compute_value_area — die zuvor in
    SessionProfile UND WeeklyProfile duplizierte Maschinerie (VAP-Akkumulation,
    Regime-Heuristik, POC-Drift, Archivierung) existiert genau einmal
  - SessionProfile + WeeklyProfile sind jetzt Subklassen; Output-Keys und
    Verhalten identisch (Contract-Proof PASS, alle Alt-Tests gruen)
  - core/session_profile.py re-exportiert BUCKET/ProfileSnapshot/Helpers —
    bestehende Importe (composite_profile, tests) unveraendert
  - Verification: verify_pr4a_contract PASS (nur additive Keys),
    verify_pr4a_isolation PASS, verify_pr4a_perf PASS (10k Trades 464ms)

## Sprint B2 (Interpretations-Layer, Methodology Steps 5-8) — COMPLETE 2026-07-02

Kanonische Methodik-Referenz: docs/METHODOLOGY_STEPS_5-8.md
(System-Layering: Pipeline -> Struktur -> Interpretation -> Strategie -> Validierung;
alle Layer-3-Module sind reine Funktionen ueber Profildaten = backtestbar)

### Step 5 — core/profile_structure.py (NEU)
  - find_single_prints(vap): innere Buckets <= 5% POC-Volumen, Raender (Tails)
    ausgenommen — vom Markt "geschuldete" Repair-Zonen
  - classify_extremes(vap): duennes Extrem (Rand/Koerper <= 0.25) -> "strong"
    (institutionelle Ablehnung), fettes Extrem (>= 0.75) -> "weak" (poor
    high/low, Revisit wahrscheinlich)
  - detect_double_distribution(vap): Shape "B" via profile_shape + Bruecke
  - structure_context(vap): flacher Feature-Vektor
  - tests/test_profile_structure.py — 29 Asserts

### Step 6 — core/business_zones.py (NEU)
  - build_zones(profiles): POC/VAH/VAL/HVN/Single-Print-Zonen aus archivierten
    ProfileSnapshots (Session + Weekly); Merge gleicher Art; recurrence =
    Anzahl bestaetigender Profile = Staerke
  - ZoneRegistry: Live-Zustand untested -> tested -> repaired; Repair =
    kumulativ durchlaufener Bereich deckt die ganze Single-Print-Zone
    (mehrere Preis-Segmente moeglich); Zustand ueberlebt rebuild()
  - nearest_zones(): Point A -> Point B Pfad
  - tests/test_business_zones.py — 33 Asserts

### Step 7 — core/road_map.py (NEU)
  - build_road_map(session_ctx, weekly_ctx, zones_ctx, price) — rein (P3)
  - day_type: balance | trend_up | trend_down | transition | conflicted |
    neutral aus Session- + Weekly-Regime (Konflikt Session vs Weekly ->
    conflicted -> stand_aside)
  - SETUP_MATRIX: balance -> fade_edge_counter; trend -> continuation +
    pullback_to_value; transition -> wait; conflicted/neutral -> stand_aside
  - Zonen-Geschwindigkeit: single_print -> fast, hvn/poc -> rotation,
    vah/val -> reaction
  - tests/test_road_map.py — 35 Asserts

### Aggregator-Wiring
  - Neue additive AGGREGATED Felder: "profile_structure" (laufende Session),
    "business_zones" (Registry-Snapshot, Top-12), "road_map"
  - Zonen-Rebuild nur wenn neue Profile archiviert wurden (nicht pro Publish)
  - Alle drei per try/except isoliert (throttled warn, CONTEXT_FALLBACK)
  - Contract PASS, Isolation PASS, Perf PASS (10k Trades 215ms, p99 1.4ms)

### Backlog-Nachtrag (2026-07-02): Lethargy + VPOC-Trend + Inside Bar + Prompt
  - core/lethargy.py (NEU): detect_lethargy() — 3-dim. Signatur (Volume Decay,
    Range Compression, Speed Decay), Lethargie nur wenn >=2 Dimensionen UND
    Preis an Zone; LethargyDetector stateful wrapper
  - core/vpoc_trend.py (NEU): classify_vpoc_trend() — lineare Regression ueber
    Weekly-VPOC-Serie, rising/falling/flattening, Konsistenz-Score
  - core/candle_classifier.py: INSIDE_BAR (Prioritaet 5, nach Pinbar)
  - agents/signal_agent.py: Road Map + VPOC Trend + Lethargy im Prompt
  - core/profile_config.py (NEU): ProfileConfig — zentrale Schwellwerte fuer
    den gesamten Profil-Stack (bucket_size, value_area_pct, etc.)

### Review-Fixes (2026-07-02) — nach Code Review der obigen Nachtraege
  - **Kritisch**: ProfileConfig-Umbau hatte Konstruktor-Kwargs entfernt
    (SessionProfile(value_area_pct=...), ZoneRegistry(max_zones=...) etc.
    warfen TypeError) — 4 Testdateien rot. Fix: core/profile_config.py
    resolve_config() Helper, alle betroffenen __init__ akzeptieren wieder
    die alten Feld-Kwargs zusaetzlich zu config=
  - **Hoch**: test_candle_classifier.py/test_lethargy.py/test_vpoc_trend.py
    nutzen pytest-Klassen ohne __main__-Guard — `python tests/test_X.py`
    (Repo-Konvention) fuehrte 0 Tests aus, exit 0 (falsches Gruen). Fix:
    sys.path.insert + `pytest.main([__file__])` Guard ergaenzt
  - **Mittel**: LethargyDetector.set_zone() war totes Feature (ingest() las
    self._zone_low/_zone_high nie zurueck); aggregator_agent.py griff
    stattdessen direkt auf lethargy._prices/_volumes/_timestamps zu. Fix:
    ingest()/neue assess()-Methode nutzen set_zone() als Fallback;
    Aggregator ruft jetzt lethargy.set_zone() + lethargy.assess() auf
  - **Mittel**: Zonen-Rebuild-Cache verglich len(get_archived_profiles()) —
    einmal die Archiv-Ringpuffer voll (Session maxlen=60, Weekly=52) aendert
    sich len() nicht mehr, Zonen-Rebuild haette im Dauerbetrieb nach ~10
    Tagen bis zu eine Woche stillstehen koennen. Fix: BaseVolumeProfile
    bekommt monotonen archived_total_count (ueberlebt Ringpuffer-Eviction)
  - Verifiziert: 300 passed / 3 skipped (pytest, gesamtes tests/), Contract
    PASS, Isolation PASS, Perf PASS (10k Trades 486ms, p99 3.8ms)

### Naechster Schritt: Sprint C (Strategy Engine) — Strategien konsumieren
### jetzt zones/road_map/structure als Features; Backlog: ~~Lethargy-Detektor~~,
### ~~Inside Bar~~, ~~Weekly-VPOC-Trendserie~~, ~~Signal Agent Prompt Erweiterung~~

## Sprint B3 (Backlog-Abarbeitung) — COMPLETE 2026-07-02

Vier dokumentierte Backlog-Items aus Step 8 / Methodology abgearbeitet:

### 1. Lethargy Detector (core/lethargy.py)
  - detect_lethargy() — reine Funktion (P3): 3-dimensionale Lethargie-Signatur
    (Volume Decay + Range Compression + Speed Decay), min 2/3 Dimensionen +
    Zone-Proximity-Gating
  - LethargyDetector — stateful wrapper mit Rolling Buffers
  - Wiring: aggregator_agent.py ingested jeden Trade, publish_loop rechnet
    Lethargy gegen nearest Zone
  - tests/test_lethargy.py — 15 Tests (pure function + stateful)

### 2. Inside Bar im Candle Classifier (core/candle_classifier.py)
  - INSIDE_BAR als neuer Candle-Typ (Priority 5: nach Pinbar, vor Normal)
  - CandleClassifier trackt prev_high/prev_low fuer Containment-Check
  - is_indecision=True fuer Inside Bar (zusammen mit Doji/Pinbar)
  - Kein Benchmark-Feed (Inside Bars sind keine "strong" candles)
  - 10 neue Tests in test_candle_classifier.py (TestInsideBar class)

### 3. Multi-Week VPOC Trend Series (core/vpoc_trend.py)
  - build_vpoc_series() — extrahiert VPOC-Zeitreihe aus ProfileSnapshots
  - classify_vpoc_trend() — reine Funktion: Linear Regression, Richtungs-
    Klassifikation (rising/falling/flattening), Konsistenz-Score, Slope $/Woche
  - Wiring: aggregator_agent.py publish_loop → vpoc_trend Feld im AGGREGATED
  - tests/test_vpoc_trend.py — 22 Tests

### 4. Road Map + Zones im Signal Agent Prompt
  - _format_zones() — Zone-At/Below/Above + Unrepaired Single Prints
  - _format_road_map() — Day Type, Dominant Direction, Allowed Setups, Point A→B
  - _format_lethargy() — Lethargy-Status + zerfallende Dimensionen
  - _format_vpoc_trend() — Multi-Week VPOC Richtung + Staerke
  - Prompt-Instruktion erweitert: Road Map + VPOC Trend + Lethargy Kontext

### Bugfix: business_zones.py MERGE_GAP_BUCKETS → 1 (war undefiniert)

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
Sprint B: COMPLETE — Delta Divergenz + Session-Phasen + IB + Profil-Konsolidierung (65 neue Asserts)
Sprint B2: COMPLETE — Interpretations-Layer Steps 5-8 (Struktur + Zonen + Road Map, 97 neue Asserts)
Sprint B3: COMPLETE — Backlog-Abarbeitung (Lethargy + Inside Bar + VPOC Trend + Signal Prompt, 104 neue Tests)
Sprint D (Options & Pre-Session Briefing): COMPLETE — Deribit Chain + GEX Surface + Pre-Session Briefing-Karte ("Die Karte") + Event Layer + Hysterese-Filter + Restzeit-Decayed 1σ (25 Tests)
Sprint E (Risk & Challenge Geometry): COMPLETE — Multi-Barrier Tranches (P(T1), P(T2|T1)) + Regime-Conditioned Monte Carlo (4000 Paths) + Prop Presets + Shared Price Axis Sync

## Sprint D & E: Options-Layer, Pre-Session Briefing & Risk Engine — COMPLETE (2026-08)

### 1. Options Agent & Dealer Gamma Positioning (`src/core/optionsAgent.ts`)
- **Deribit Options Chain Aggregation**:
  - Live & Mock Polling für BTC-Optionen mit Strike-, Expiry- und Type-Splits (Call/Put).
  - **Net GEX ($/pt)**: GEX-Berechnung über Black-Scholes-Gamma unter inverser $S^3$- bzw. $S^2$-Hedging-Struktur ($GEX = \sum \Gamma_i \times OI_i \times S^2 \times 100 \times \text{DealerSign}$).
  - **Zero-Gamma (Gamma-Flip)**: Spot-Level, bei dem $\text{Net GEX} = 0$ durchläuft.
  - **Key Walls**: Put Wall (Peak Put-Gamma) & Call Wall (Peak Call-Gamma).
  - **25Δ-Skew**: Vol-Punkte-Differenz ($IV_{25\Delta\text{ Call}} - IV_{25\Delta\text{ Put}}$) zur Erfassung von Tail-Risk-Prämien und OTM-Schiefe.
- **Hysterese-Filter auf GEX-Regime**:
  - Verhindert Flackern am Flip-Punkt: Umschaltung zwischen `AMPLIFYING` (Short $\Gamma$) und `DAMPENING` (Long $\Gamma$) erfolgt erst nach Durchbruch über eine Pufferzone von $\pm 0.25\sigma$ der verbleibenden Session ($\min \$150$).
- **Restzeit-Decayed Expected Move**:
  - $1\sigma$-Korridor berechnet mit $\sqrt{t_{\text{rest}}/365}$ bis zum nächsten 08:00 UTC Settlement (inkl. 15-Minuten Floor) statt starrer 24h-Session-Spannweite.
  - Bereitstellung von $1\sigma$ (68% Terminal-Containment, ~32% Touch-Probability) und $2\sigma$ (95% Terminal-Containment).
- **Session-Open Anker & Drift-Differenzierung**:
  - Snapshot der Referenzwerte bei Session-Start (Spot, Zero-$\Gamma$, IV, Walls).
  - Automatische Trennung zwischen **`INFORMATIVEM OI-AUFBAU`** (Zero-$\Gamma$ oder Walls verschieben sich bei ruhigem Spot) und **`MECHANISCHEM SPOT-DRIFT`** (Verschiebung nur als Derivat der Spot-Bewegung).
  - `/risk/reset-anchor` Endpoint für Re-Anchoring.
- **Änderungsliste (Material Change Feed)**:
  - Ereignisbasiertes Log signifikanter Shifts ($\Delta \text{Zero-}\Gamma > \$250$, Wall-Sprünge, $\Delta \text{IV} > 1.0\%$, Regime-Wechsel).

### 2. Pre-Session Briefing-Karte („Die Karte“ · `static/risk.html`)
- **Strikter Scope**: Zeigt ausschließlich Level und Positionierungsdaten, die im klassischen Chart **nicht** sichtbar sind:
  - **Session-Korridor 1σ**: $S \times IV \times \sqrt{t_{\text{rest}}/365}$ mit Restzeit-Anzeige und Guardrail (*Keine Touch-Grenze*).
  - **Gamma-Flip**: Umschlagpunkt des Dealer-Hedgings (*Kein statischer Support/Resistance*).
  - **Call- / Put-Wall**: Zonen mit maximalem Hedging-Pinning.
  - **Regime**: Long $\Gamma$ vs. Short $\Gamma$ mit Hysterese-Bandbreite (*Keine Richtungsindikation*).
  - **RV vs. IV & 25Δ Skew**: Verhältnis von realisierter zu impliziter Volatilität.
  - **Liquidation-Cluster**: 25x, 50x und 100x Hebel-Pools.
- **Deterministischer Event-Layer**:
  - **08:00 UTC Deribit Daily Expiry**: Countdown & Auswertung der empirischen Doppelbedingung (ATM-OI Top-Dezil + Net $\Gamma < 0$).
  - **Freitag 08:00 UTC Weekly / Monthly Expiry**: Höheres OI, belastbarere Walls.
  - **8h Funding-Resets**: 00:00, 08:00, 16:00 UTC Fenster.
  - **Prop-Risk Resets**: Breakout (00:30 UTC), FundingPips (17:00 EST / 21:00 UTC).
- **Struktur-Besonderheit bei BTC-Optionen**:
  - Hinweis zur Marktstruktur: Miner/Treasury Covered Calls (Dealer Long Calls) vs. Retail Call Buying (Dealer Short Calls); Gamma-Flip als Orientierungsmarke, nicht als binärer Schalter.

### 3. Multi-Barrier Scale-Out Geometry & Monte Carlo Engine (`src/core/geometry.ts`)
- **Analytische First-Passage GBM Identitäten**:
  - $P(T_1) = \frac{1 - e^{-2\mu d_s / \sigma^2}}{e^{2\mu d_{t1} / \sigma^2} - e^{-2\mu d_s / \sigma^2}}$ (mit Grenzübergang $\frac{d_s}{d_s + d_{t1}}$ für $\mu \to 0$).
  - Bedingte Zweitbarrieren-Wahrscheinlichkeit $P(T_2 \mid T_1)$ mit Breakeven-Stop-Trail.
  - Disjunktes Ergebnis-Trio: Voll-Gewinn ($T_1 + T_2$), Teil-Gewinn ($T_1$ gefolgt von BE-Stop), Voll-Verlust (Initial Stop).
- **Regime-Conditioned Monte Carlo Simulator**:
  - 4.000 vektorisierte Pfad-Simulationen (Challenge-Modus).
  - Volatilitäts-Multiplikatoren konditioniert auf GEX-Regime ($1.30\times$ im Amplifying Regime, $0.85\times$ im Dampening Regime) und $25\Delta$-Skew-Drift.
  - Diskrete Bust-Kategorien (Trailing Drawdown vs. Daily Loss Limit) und Pass-Wahrscheinlichkeiten.
- **Prop Firm Preset Engine**:
  - Native Unterstützung für Breakout (Static 4% Max DD / 2.5% Daily), FundingPips (Trailing EOD 6% DD), Apex Trader, TopStep, FTMO.
  - Dynamisches Positions-Sizing mit Hebel-Kappung bei engen Stops.

### 4. Shared Price Axis (SVG Interactive Axis)
- Visuelle Verankerung aller Options- und Orderflow-Level:
  - Dynamische $1\sigma / 2\sigma$ Decayed Expected Move Bänder.
  - Zero-Gamma-Linie mit visuellem Hysterese-Pufferband.
  - Interaktive Zieh-Handles für **Stop**, **Entry**, **Target 1 (Tranche 1)** und **Target 2 (Tranche 2)** mit synchroner Slider- und Parameter-Rückkopplung.
  - Liquidation Clusters (25x, 50x, 100x).

### 5. Verifikation & Tests (`src/tests/test_geometry_and_options.ts`)
- 25 automatisierte Unit- und Integrationstests (GBM First-Passage Identitäten, Breakeven RRR, Scale-Out Wahrscheinlichkeiten, Prop-Sizing, Challenge Monte Carlo, Options GEX Modeling, $25\Delta$-Skew).
- Ausführung: `npm test` oder `npx tsx src/tests/test_geometry_and_options.ts`.

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