# OrderFlow Pro

Deskriptives Informationssystem für die Vorbereitung und Begleitung
diskretionärer BTC-Day-Trading-Sessions.

Es beantwortet eine Frage: **Wie ist der Markt aufgestellt?** Nicht wohin er
geht, nicht was gehandelt werden soll. Der Read gehört dem Trader; das System
liefert, was im Chart nicht sichtbar ist — Dealer-Positionierung, die vom
Optionsmarkt eingepreiste Spanne und Zwangsflüsse.

Verbindlich ist der Charter (`orderflow-pro als Informationssystem`).
Bei Widerspruch gilt er, nicht diese Datei.

## Start

```bash
pip install -r requirements.txt
python main.py
```

- Dashboard: http://127.0.0.1:8000
- Pre-Session-Karte: http://127.0.0.1:8000/risk

Der Browser öffnet nach zwei Sekunden von selbst.

**Windows:** `tzdata` wird über `requirements.txt` mitinstalliert. Fehlt es,
läuft alles weiter, aber der FundingPips-Reset rechnet mit festem UTC-5 und
liegt während der Sommerzeit eine Stunde daneben — die Karte schreibt das an
den Termin.

**Ohne Netz zu Binance oder Deribit** startet das System trotzdem. Es zeigt
dann Leerzustände statt Zahlen; das ist Absicht (Charter §2).

## Was die Karte zeigt

| Kachel | Inhalt |
|---|---|
| Dealer-Hedging | Long / Short Gamma / Transition, mit Hysterese gegen Flackern |
| Gamma-Flip | Preis mit Netto-GEX = 0, plus Delta zum Sessionanker |
| Put- / Call-Wall | Größte Gamma-Konzentrationen |
| Eingepreiste Spanne | 1σ / 2σ über √(Restzeit bis 08:00-UTC-Reset), beide Wahrscheinlichkeitslesarten |
| IV gegen RV | Implizite gegen realisierte Volatilität |
| Liquidationscluster | Noch nicht verfügbar — verlangt Perp-OI je Preisniveau |
| Level-Bewegung | Mechanisch (Spot lief) gegen informativ (OI bewegt) getrennt |
| Anstehend | Verfälle, Funding-Resets, Makro-Fenster, Prop-Resets mit Countdown |
| Änderungen | Nur materielle, seit Sessionstart |
| Annahmen | Die vier Grenzen aus Charter §7, aufklappbar |

Darunter das Risikoblatt: Barrier-Geometrie, Position Sizing, Prop-Challenge-
Simulation.

## Architektur

Ein Datenpfad, ein Stack. Agents kommunizieren ausschließlich über den Broker.

```
Exchange Agent (Binance WS)  ─┐
                              ├─→ Aggregator ─→ Display Agent ─→ Browser
Options Agent (Deribit REST) ─┘                       ↑
                                                 Logger Agent
```

```
main.py                     Orchestrierung aller Agent-Loops
agents/
  exchange_agent.py         Binance L2 + Trades via ccxt.pro
  aggregator_agent.py       CVD, Delta, Imbalance, Kontext-Layer
  options_agent.py          Deribit-Kette, BS-Gamma, GEX, Zero-Γ, Walls,
                            08:00-UTC-Reversal-Flag, Zwei-Uhren-Zerlegung
  display_agent.py          FastAPI + WebSocket; /risk/state, /risk/evaluate
  logger_agent.py           Parquet + JSONL
core/
  broker.py                 asyncio.Queue Message Bus
  session_corridor.py       Eingepreiste Spanne über Restzeit
  event_layer.py            Verfälle, Funding, Makro, Prop-Resets
  regime_state.py           Hysterese, Session-Anker, Änderungsliste
  volume_profile.py         VAP/POC/Value Area (Basis für Session + Weekly)
  business_zones.py         Zone Registry, road_map.py, profile_structure.py
  orderbook.py, cvd.py, divergence.py, absorption.py, lethargy.py, ...
strategies/
  geometry.py               Barrier-Mathematik (GBM First-Passage)
  base.py                   PropFirmRules, Sizing, Challenge-Simulation
  backtest.py               vectorbt + Optuna (optional, siehe unten)
static/
  index.html                Live-Terminal
  risk.html                 Pre-Session-Karte + Risikoblatt
docs/
  GEX_SCALING.md            S² gegen S³ — offene Entscheidung
  CHAIN_SOURCE.md           Vertrag für Ersatz-Datenquellen
  METHODOLOGY_STEPS_5-8.md  Methodik-Referenz
```

Kein LLM im Analyse-, Risiko- oder Execution-Pfad. Alles deterministisch.

## Tests

```bash
pip install -e ".[dev]"
pytest                                    # 381 Tests
python scripts/verify_pr4a_contract.py    # Payload-Vertrag
python scripts/verify_pr4a_isolation.py   # Fehlerisolation je Modul
python scripts/verify_pr4a_perf.py        # Durchsatz + Publish-Latenz
```

`tests/test_sprint1.py` ist ausgenommen — es spricht beim Sammeln die
Live-Binance-API an. Explizit anfordern mit `pytest tests/test_sprint1.py`.

## Backtesting (optional)

`strategies/backtest.py` braucht zusätzliche Pakete, die nicht Teil des
Live-Betriebs sind:

```bash
pip install vectorbt optuna
python scripts/evaluate.py
```

## Historische Daten

```bash
python scripts/download_history.py --days 30   # ~107M Trades
python scripts/replay_history.py               # -> data/training_features.parquet
```

## Offen

- GEX-Skalierung S² gegen S³ — Herleitung in `docs/GEX_SCALING.md`,
  Verifikation mit `python scripts/gex_scaling_probe.py`
- Liquidationscluster aus Perp-OI (Kachel steht, Daten fehlen)
- Endlicher Horizont in der Geometrie (`p_timeout` ist konstant 0)
- `session_corridor.target_position()` ist gebaut, aber noch nicht an das
  Target-Feld des Risikoblatts gehängt
- Die Vorschlags-Engine in `risk.html` widerspricht Charter §2
