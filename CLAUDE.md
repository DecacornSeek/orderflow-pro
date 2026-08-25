# OrderFlow Pro — Master Context

## Zweck

Ein **deskriptives Informationssystem** neben TradingView. Zweiter Bildschirm,
nicht Ersatz.

Es beantwortet eine Frage: **Wie ist der Markt aufgestellt?**

Nicht: wohin er geht. Nicht: was gehandelt werden soll. Nicht: ob ein Setup
gut ist.

Der Trader liest Struktur klassisch (NCI, Heatmap, Absorption, Fibonacci) und
setzt Richtung, Entry, Stop und Target selbst. Das System liefert
ausschliesslich die Information, die **im Chart nicht sichtbar ist**:
Dealer-Positionierung, die vom Optionsmarkt eingepreiste Spanne und
Zwangsfluesse.

Kanonisch ist der Charter (`orderflow-pro als Informationssystem`). Bei
Widerspruch gilt er, nicht diese Datei.

## Was das System nicht tut

| Nicht | Grund |
|---|---|
| Keine Richtungsprognose, kein BULLISH/BEARISH-Label | Die ehrliche Zahl ist 50% minus Gebuehren. Ein Richtungslabel waere eine Behauptung ohne Schaetzer. |
| Kein Drift aus Skew | Skew lebt unter Q (Absicherungskosten, Varianzrisikopraemie), nicht unter P. Default: mu = 0. |
| Keine Signalgenerierung, kein Entry-Vorschlag | Der Read gehoert dem Trader. |
| Kein LLM im Analyse-, Risiko- oder Execution-Pfad | Alles deterministisch. |
| **Kein Backtest-Framework, keine Strategie-Entwicklung** | Anderer Zweck, andere Architektur. Entfernt am 2026-08-25. |
| Kein Soft-Stop | Prop-Firm-Daily-Loss-Limits sind hart. Nur harte Invalidierungslevel. |
| Kein paralleler Stack | Alles in FastAPI + `DisplayAgent` + statischem Frontend. |
| **Keine angezeigte Zahl ohne Datengrundlage** | Fehlt ein Wert, erscheint der Leerzustand mit Begruendung — nie eine 0, nie ein Schaetzwert. |

Die einzige Ausnahme zur Richtungsneutralitaet: das Return-Reversal um die
taegliche 08:00-UTC-Deribit-Expiry (Weiss et al. 2026, FRL,
doi 10.1016/j.frl.2026.110340). Signifikant nur bei ATM-OI im obersten Dezil
UND negativem Net Gamma. Als Flag darstellen, nicht als Empfehlung. Alle
Bedingungen muessen erfuellt sein, sonst inaktiv. Das Flag leuchtet selten —
das ist beabsichtigt.

## Zwei Betriebsmodi

**Pre-Session-Briefing** — kompakte, scrollfreie Karte vor Sessionbeginn.
Regime, Gamma-Flip, Walls, impliziter Korridor 1σ/2σ, RV gegen IV,
Liquidationscluster, Event-Layer. Ihr Wert: der Trader sieht sofort, ob sein
aus dem klassischen Read gesetztes Target innerhalb oder ausserhalb der
eingepreisten Verteilung liegt — und wie viel Zeit er dafuer braucht.

**In-Session-Aktualisierung** — die Level bewegen sich aus zwei Gruenden, und
**diese Unterscheidung ist die zentrale Designentscheidung**:

- *mechanisch* — Spot bewegt sich, Gamma rechnet sich neu, Zero-Γ verschiebt
  sich. Die Positionierung hat sich nicht geaendert, nur der Standpunkt.
  Rauschen.
- *informativ* — Spot steht, aber Zero-Γ oder Walls wandern. Es wurde
  tatsaechlich OI auf- oder abgebaut. **Das ist das Signal.**

Werden beide gleich dargestellt, ist der zweite Fall unsichtbar.

## Zeit ist eine erste Dimension

Der Korridor rechnet mit sqrt(Restzeit bis Reset), nicht mit
sqrt(Sessionlaenge). Eine Karte, die den ganzen Tag dieselbe Spanne zeigt, ist
ab Mittag zu weit und laesst Ziele erreichbar aussehen, die es zeitlich nicht
mehr sind.

## Stabilitaet der Anzeige

Eine flackernde Anzeige ist schlechter als keine.

- **Hysterese** auf den Regime-Zustand: Umschaltung erst bei Durchbruch ueber
  ein Totband UND Verweildauer. Der Weg zurueck ins Totband ist dagegen
  sofort — Unsicherheit einzuraeumen braucht keine Wartezeit, eine Behauptung
  schon.
- **Session-Open als Anker**: Snapshot bei Sessionstart, aktuelle Werte als
  Delta dazu.
- **Aenderungsliste statt Daueranimation**: nur materielle Aenderungen.

## Bekannte Grenzen — im UI benannt, nicht versteckt

Stehen als `LIMITS` in `agents/display_agent.py`, damit sie nur einmal
existieren, und erscheinen aufklappbar in der Karte:

1. **68% ist Terminal-Containment, keine Beruehrungsgrenze.** Die
   Wahrscheinlichkeit, das obere 1σ-Band waehrend der Session zu beruehren,
   liegt bei rund 32% (Reflexionsprinzip).
2. **Das GEX-Vorzeichen haengt an einer Annahme ueber die Halterseite.** Bei
   BTC steht Covered-Call-Writing von Minern und Treasuries gegen
   Retail-Call-Buying. Der Flip ist eine Orientierungsmarke, kein Schalter.
3. **Walls markieren, wo Hedging klebt — nicht, wo der Preis dreht.**
4. **Gamma-Floor bei T gegen 0.** Ohne Floor springt die Anzeige kurz vor
   Settlement in einen Extremzustand, der ein Artefakt der Formel ist.
   Gerechnet wird mit mindestens 15 Minuten Restlaufzeit.

## Architektur

Ein Datenpfad. Agents kommunizieren ausschliesslich ueber den Broker.

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
                            Reversal-Flag, Zwei-Uhren-Zerlegung
  display_agent.py          FastAPI + WebSocket; /risk/state, /risk/evaluate
  logger_agent.py           Parquet + JSONL
core/
  broker.py                 asyncio.Queue Message Bus
  session_corridor.py       Eingepreiste Spanne ueber Restzeit
  event_layer.py            Verfaelle, Funding, Makro, Prop-Resets
  regime_state.py           Hysterese, Session-Anker, Aenderungsliste
  volume_profile.py         VAP/POC/Value Area (Basis Session + Weekly)
  business_zones.py         Zone Registry (untested/tested/repaired)
  road_map.py               Tagestyp aus Session- und Weekly-Regime
  profile_structure.py      Single Prints, schwache/starke Extreme
  orderbook.py, cvd.py, divergence.py, absorption.py, lethargy.py,
  vpoc_trend.py, candle_classifier.py, pattern_engine.py, ...
risk/                       KEIN Strategie-Paket
  geometry.py               Barrier-Mathematik (GBM First-Passage)
  base.py                   PropFirmRules, Sizing, Challenge-Simulation
static/
  index.html                Live-Terminal
  risk.html                 Pre-Session-Karte + Risikoblatt darunter
docs/
  GEX_SCALING.md            S² gegen S³ — offene Entscheidung
  CHAIN_SOURCE.md           Vertrag fuer Ersatz-Datenquellen
  METHODOLOGY_STEPS_5-8.md  Methodik-Referenz
```

## Coding-Prinzipien

- Immer asyncio, nie blocking code
- Redis/Broker ist der einzige Kommunikationskanal zwischen Agents
- Fehler loggen, nie crashen — Reconnect-Logik immer dabei
- Kommentare auf Deutsch
- Layer-3-Module sind reine Funktionen ueber Profildaten
- Kein Ersatzwert, wenn ein Messwert fehlt: `None` nach oben durchreichen und
  in der Anzeige als Leerzustand mit Begruendung zeigen

## Datenquelle

Primaer Deribit direkt. Eine Ersatzquelle muss die **rohe Kette** liefern (OI
je Strike + IV), niemals vorberechnetes GEX — sonst bricht die
Zwei-Uhren-Zerlegung, die Halterseiten-Annahme waere die des Anbieters und
der Gamma-Floor ebenfalls. Details in `docs/CHAIN_SOURCE.md`.

Die Boerse selbst darf nicht wechseln: der 08:00-UTC-Koeffizient ist auf
Deribit-Verfaellen geschaetzt.

## Stand

Gebaut und verifiziert (381 Tests):

- Deribit-Options-Layer: GEX, Zero-Γ, Walls, Expiry-Gruppen, Reversal-Flag
- Zwei-Uhren-Zerlegung mechanisch gegen informativ
- Session-Korridor mit Restzeit-Skalierung, beide Wahrscheinlichkeitslesarten
- Event-Layer mit Countdowns inkl. Prop-Resets
- Regime-Hysterese, Session-Anker, Aenderungsliste
- Pre-Session-Karte in `static/risk.html`, scrollfrei
- Kontext-Layer: Session-/Weekly-Profile, Zonen, Road Map, Struktur,
  Divergenz, Absorption, Lethargy, VPOC-Trend

Offen:

- GEX-Skalierung S² gegen S³ (`docs/GEX_SCALING.md`, Verifikation mit
  `scripts/gex_scaling_probe.py` auf einem Rechner mit Deribit-Zugang)
- Liquidationscluster aus Perp-OI — Kachel steht, Daten fehlen
- Endlicher Horizont in `risk/geometry.py`: `p_timeout` ist konstant 0
- `session_corridor.target_position()` ist gebaut, aber noch nicht an das
  Target-Feld des Risikoblatts gehaengt
- Die Vorschlags-Engine in `risk.html` schlaegt Entry/Stop/Target vor und
  widerspricht damit dem Charter

## Aenderungshistorie der Ausrichtung

**2026-08-25 — Strategie-Teil entfernt.** Das Projekt war als Signal- und
Backtesting-System angelegt (DeepSeek Signal Agent, vectorbt + Optuna
Walk-Forward, Trainings-Features mit Preis-Labels, ChromaDB Pattern Memory).
Diese Ausrichtung ist aufgegeben — es soll ein Informationssystem neben
TradingView sein, kein Strategie-Labor.

Entfernt: `agents/signal_agent.py` (LLM im Analysepfad),
`strategies/backtest.py` (vectorbt + Optuna), `scripts/evaluate.py`,
`scripts/replay_history.py` (labelte Punkte mit dem zukuenftigen Preis, also
Richtungsprognose), Abhaengigkeit `openai`.

Umbenannt: `strategies/` → `risk/`. Was bleibt, ist Risikomanagement und
Barrier-Mathematik fuer eine vom Trader gesetzte Geometrie.

`SYSTEM_ARCHITECTURE_V2.md` beschreibt in Teilen noch die alte Ausrichtung
(Optimizer, Strategie-Engine, Hermes). Nicht geloescht, weil dort auch
unbebaute Kontext-Features stehen, die weiterhin passen: Funding-Rate + OI,
CME-Gap, Coinbase Premium Index.

**Frueher (weiterhin gueltig):** Broker als asyncio.Queue lokal, Redis auf
dem VPS — Agent-Interfaces identisch, Migration ohne Agent-Code-Aenderung.
