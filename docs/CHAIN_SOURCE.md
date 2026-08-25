# Datenquelle der Optionskette — Vertrag

Primaerquelle ist Deribit direkt (`agents/options_agent.py`,
`fetch_deribit_chain_sync`). Dieses Dokument haelt fest, was eine
*Ersatzquelle* liefern muss, falls Deribit einmal nicht erreichbar ist.

## Die Regel

**Beziehe die Kette, nicht die Schlussfolgerung.**

Zulaessig ist Rohmaterial: Open Interest pro Strike, Strike, Verfall,
Optionstyp, Mark-IV. Nicht zulaessig als Primaerquelle ist vorberechnetes
GEX, ein fertiger Zero-Gamma-Level oder fertige Walls.

Bei Anbietern, die beides fuehren, ist also der OI-pro-Strike-Endpunkt zu
nehmen, nicht der GEX-Endpunkt.

## Warum — drei Gruende, alle spezifisch fuer dieses System

### 1. Die Zwei-Uhren-Zerlegung braucht die rohe Kette

Charter §4.2 ist die zentrale Designentscheidung: eine Level-Verschiebung
durch Spot-Bewegung ist Rauschen, dieselbe Verschiebung bei stehendem Spot ist
das Signal. `OptionsAgent.compute_chain_shift()` trennt das, indem es **beide
Ketten auf denselben Spot rechnet** — die alte Kette wird auf den aktuellen
Spot nachgerechnet, und was dann an Differenz bleibt, kann nicht vom Spot
kommen.

Diese Operation setzt voraus, dass eine Kette *nachrechenbar* ist. Kommt
stattdessen fertiges GEX im Takt des Anbieters, existiert kein Zustand, den
man bei festgehaltenem Spot neu bewerten koennte. Die Trennung ist dann nicht
schlechter, sondern strukturell nicht mehr herstellbar.

`tests/test_chain_shift.py` haelt die Eigenschaft fest: bei reiner
Spot-Bewegung ist der informative Anteil exakt null; mechanischer und
informativer Anteil addieren sich zur beobachteten Gesamtverschiebung.

### 2. Die Halterseiten-Annahme waere die des Anbieters

Das GEX-Vorzeichen haengt an der Annahme, welche Seite die Dealer halten
(Charter §7.2). Bei BTC steht Covered-Call-Writing von Minern und Treasuries
gegen Retail-Call-Buying — das Vorzeichen ist schwaecher bestimmt als bei SPX.
Weicht die Konvention des Anbieters von der eigenen ab, bedeutet das Feld
`gex_regime` etwas anderes, ohne dass es auffaellt. Die Annahme steht hier in
`calculate_gex_usd()`: Call = +1, Put = −1.

### 3. Der Gamma-Floor bei T→0 ist die eigene Behandlung

`EXPIRY_FLOOR_SECONDS = 900` verhindert, dass Gamma kurz vor Settlement
divergiert (Charter §7.4). Wie ein Anbieter mit T→0 umgeht, ist dessen
Entscheidung und meist nicht dokumentiert — genau in der Stunde vor 08:00 UTC,
in der die Karte am meisten zaehlt.

## Fertiges GEX als Kreuzcheck

Als *zweite* Quelle ist vorberechnetes GEX wertvoll: laeuft es gegen die
eigene Rechnung, steht die eigene Annahme gegen die Marktkonvention. Das ist
eine Information ueber die Annahme, nicht ueber den Markt — und gehoert
entsprechend beschriftet.

Dasselbe gilt fuer die Skalierungsfrage aus `docs/GEX_SCALING.md`.

## Was eine Ersatzquelle technisch liefern muss

`parse_chain(raw_chain, fallback_spot)` erwartet je Instrument:

```
{
  "instrument_name": "BTC-27JUN26-90000-C",   # oder Strike/Verfall/Typ einzeln
  "open_interest":   <float, in BTC>,
  "mark_iv":         <float, Prozent, z.B. 55.8>,
  "underlying_price": <float, optional>
}
```

Ein Adapter muss also nur auf dieses Format abbilden. Alles danach — Gamma,
GEX, Zero-Gamma, Walls, Expected Move, Zerlegung — bleibt unveraendert
eigener Code. Der Anbieter wechselt dann die Quelle, nicht die Bedeutung.

**Nicht** zu wechseln ist dabei die Boerse: Der 08:00-UTC-Koeffizient aus
Weiss et al. (2026) ist auf Deribit-Verfaellen geschaetzt. Eine Ersatzquelle,
die Deribit-Daten weiterreicht, laesst das Flag gueltig; eine andere Boerse
tauscht den zugrunde liegenden Markt aus und damit die Grundlage des Flags.

## Kein Aufbau um Sandbox-Grenzen herum

Die Entwicklungsumgebung dieses Repos kann `www.deribit.com` nicht erreichen
(Egress-Policy, 403 auf CONNECT). Das ist eine Eigenschaft der Umgebung, keine
des Marktes. Der Produktionspfad fetcht deshalb unveraendert direkt gegen
Deribit; nur `scripts/gex_scaling_probe.py` kann alternativ aus einer Datei
lesen, damit sich offline nachrechnen laesst.
