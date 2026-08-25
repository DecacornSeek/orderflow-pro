# GEX-Skalierung bei inversen Deribit-Kontrakten — S² oder S³?

Entscheidungsvorlage zu Charter §8 („inverse S³-GEX"). Der Code rechnet aktuell
S². Dieses Dokument leitet her, warum **beide Exponenten richtig sein können**,
welcher zu welcher Gamma-Definition gehört, und warum die Wahl **nicht** nur die
Größenordnung verändert.

Status: **offen**. Der Code bleibt bis zur Entscheidung unverändert bei S².

---

## 1. Ausgangslage

Deribit-BTC-Optionen sind invers: ein Kontrakt lautet auf 1 BTC, Prämie und
Settlement erfolgen in BTC. Der Call-Payoff bei Verfall ist

    Payoff_BTC = max(S_T − K, 0) / S_T

Multipliziert mit dem Settlementpreis S_T ergibt das exakt den Payoff einer
gewöhnlichen USD-Option:

    Payoff_BTC · S_T = max(S_T − K, 0)

Daraus folgt die zentrale Identität — der **USD-Wert** einer inversen Option ist
der gewöhnliche Black-Scholes-Wert:

    V_USD = S · V_BTC

Der „inverse" Charakter steckt also nicht im USD-Wert, sondern ausschließlich
in der Denominierung. Das ist die ganze Quelle der Verwirrung um S² vs. S³.

## 2. Die Standardformel (S²) und wo sie herkommt

Für eine Option auf 1 Einheit Underlying mit USD-Wert V:

    Γ = ∂²V/∂S²                                   [1/USD]

Ein Spot-Move dS ändert das Delta um Γ·dS Einheiten Underlying. In USD bewertet
sind das Γ·dS·S. Für einen 1%-Move, also dS = 0.01·S:

    ΔDollar-Delta = Γ · 0.01·S · S = Γ · S² · 0.01

Aggregiert über Open Interest:

    **GEX_USD = Γ_BS · OI · S² · 0.01 · sign**

Das ist exakt `calculate_gex_usd()` in `agents/options_agent.py:202`. Das Gamma
dort wird selbst gerechnet (`calculate_bs_gamma`, Zeile 181) und ist Γ_BS —
USD-denominiert, Einheit 1/USD.

**Mit selbst gerechnetem Black-Scholes-Gamma ist S² die richtige Skalierung.**

## 3. Wo S³ herkommt

Deribit liefert in der eigenen API Greeks in **BTC-Denominierung**, also
Γ_BTC = ∂²V_BTC/∂S². Aus V_BTC = V/S folgt durch zweimaliges Ableiten:

    ∂V_BTC/∂S   = Δ/S − V/S²
    **Γ_BTC     = Γ/S − 2Δ/S² + 2V/S³**

Der führende Term ist Γ/S. Setzt man nur diesen ein, hebt sich das eine S gerade
weg:

    Γ_BTC · S³ · 0.01  ≈  (Γ/S) · S³ · 0.01  =  Γ · S² · 0.01

**Mit Deribits BTC-denominiertem Gamma ist S³ die richtige Skalierung.** Die
beiden Exponenten sind also keine konkurrierenden Behauptungen über die Welt,
sondern gehören zu zwei verschiedenen Gamma-Definitionen. Charter §8 beschreibt
den Deribit-Greeks-Pfad; der Code geht den selbst gerechneten Pfad.

## 4. Größenordnungs-Check

S = 100.000, σ = 55%, T = 1 Tag, ATM, pro 1 BTC Open Interest:

| Pfad | Ergebnis |
|---|---|
| Γ_BS · S² · 1% | **13.856 USD** — plausibel |
| Γ_BS · S³ · 1% | 1.385.634.913 USD — um Faktor S daneben |
| Γ_BTC · S³ · 1% | **12.868 USD** — plausibel |

Ein blindes Umstellen der bestehenden Formel auf S³, ohne gleichzeitig auf
Deribits Greeks zu wechseln, würde jede GEX-Zahl um fünf Größenordnungen
aufblähen.

## 5. Warum die Wahl trotzdem nicht egal ist

Die Abweichung zwischen beiden Pfaden ist **kein konstanter Faktor**. Die
Korrekturterme −2Δ/S² + 2V/S³ wachsen mit Moneyness und Restlaufzeit:

| K/S | 1 Tag | 7 Tage | 30 Tage |
|---|---|---|---|
| 0.80 | Vorzeichenwechsel | Vorzeichenwechsel | Vorzeichenwechsel |
| 0.90 | Vorzeichenwechsel | −86% | −68% |
| 0.95 | −66% | −35% | −49% |
| 1.00 | −7% | −19% | −37% |
| 1.05 | −3% | −12% | −30% |
| 1.10 | −2% | −9% | −25% |

Bei ITM-Calls wird Γ_BTC **negativ**. Das ist kein Rechenfehler: der BTC-Payoff
eines tief im Geld liegenden Calls ist 1 − K/S, und diese Funktion ist konkav in
S. In BTC gemessen trägt ein tief ITM-Call also negatives Gamma.

Konsequenz: Der Zero-Γ-Level verschiebt sich nicht nur, das kumulierte
GEX-Profil **ändert die Form**, weil einzelne Strikes das Vorzeichen wechseln.
Zero-Γ, Call-/Put-Wall und das Regime-Label hängen daran.

## 6. Die eigentliche Frage

Nicht „S² oder S³", sondern: **in welcher Währung ist das Buch des Dealers
margined?**

- Hedged der Dealer in USD-denominierter Exposure (USDT-Perps, CME), ist die
  USD-Sicht maßgeblich → Γ_BS, S².
- Ist das Buch BTC-margined (Deribit-Inverse-Perps, dort hedgen die meisten
  Deribit-Market-Maker tatsächlich), ist die BTC-Sicht maßgeblich → Γ_BTC, S³,
  inklusive negativem ITM-Gamma.

In der Praxis ist beides gemischt und die Aufteilung nicht beobachtbar. Das ist
dieselbe Klasse von Unschärfe wie Charter §7.2 (Halterseite): eine Annahme, die
das Vorzeichen bestimmt und die man nicht messen kann.

## 7. Empfehlung

**Primär S² mit selbst gerechnetem Γ_BS beibehalten, Charter §8 korrigieren.**

Gründe:
1. Die angezeigte Zahl ist „USD-Notional pro 1% Move" — eine USD-Größe, die der
   Trader in USD liest. Die USD-Sicht ist die, die zur Anzeige passt.
2. Selbst gerechnetes Gamma ist unabhängig von Deribits Greek-Konvention, die
   sich ändern kann und nicht versioniert ist.
3. Kein negatives ITM-Gamma, das im UI erklärt werden müsste, ohne dass ein
   Trader daraus etwas ableiten kann.

**Zusätzlich**: Γ_BTC/S³ als zweite, jederzeit abrufbare Rechnung vorhalten und
die Differenz im Zero-Γ ausweisen. Wenn beide Konventionen denselben Flip
liefern, ist die Marke belastbar; laufen sie auseinander, gehört genau das ins
UI — als Spanne, nicht als Punkt. Das ist die ehrliche Darstellung einer
Annahme, die man nicht auflösen kann.

## 8. Verifikation auf echten Daten

Deribit ist aus der Entwicklungsumgebung nicht erreichbar (Egress-Policy des
Proxys antwortet mit 403 auf `www.deribit.com:443`). Die Tabellen oben sind
analytisch, nicht aus einer Live-Kette.

Auf einem Rechner mit Deribit-Zugang:

    python3 scripts/gex_scaling_probe.py                    # Live-Fetch
    python3 scripts/gex_scaling_probe.py --save chain.json  # Kette mitschreiben
    python3 scripts/gex_scaling_probe.py --file chain.json  # offline nachrechnen

Das Skript rechnet dieselbe Kette unter beiden Konventionen durch und stellt
Net-GEX, Zero-Γ, Call-/Put-Wall und Regime-Label gegenüber. Erst diese Ausgabe
beantwortet, ob die Wahl in der aktuellen Marktlage überhaupt einen sichtbaren
Unterschied macht.

---

## 9. Nachtrag: systematische Verzerrung der BTC-Sicht

Ein Probelauf des Skripts gegen eine **nachgebaute** Kette (360 Instrumente,
4 Verfallstermine, Spot 100k — keine Marktdaten, nur zur Funktionsprüfung)
zeigte in allen drei Verfallsgruppen **gegensätzliche Regime-Labels**: USD-Sicht
long gamma, BTC-Sicht short gamma. In der BTC-Sicht existierte zudem gar kein
Nulldurchgang, also kein Zero-Γ.

Der Mechanismus dahinter ist kein Artefakt der Testdaten, sondern strukturell.
Für tief im Geld liegende Optionen dominiert in Γ_BTC der Term −2Δ/S²:

- ITM-Call: Δ → +1, Beitrag → −2K/S³, mit sign = +1 also **negativ**
- ITM-Put: Δ → −1, Beitrag → +2/S², mit sign = −1 also ebenfalls **negativ**

Beide Flügel drücken das Netto in der BTC-Sicht nach unten. Eine Konvention, die
strukturell zu „short gamma" tendiert, trägt genau dann wenig Information, wenn
das Label die eigentliche Aussage der Karte ist.

Wie stark das in der Praxis durchschlägt, hängt daran, wie viel Open Interest
tatsächlich ITM liegt — auf Deribit üblicherweise deutlich weniger als in der
Testkette, weil OI sich an runden OTM-Strikes sammelt. **Das entscheidet erst
der Lauf gegen die echte Kette.** Die Empfehlung aus §7 bleibt bis dahin
bestehen, jetzt mit einem zusätzlichen Grund.
