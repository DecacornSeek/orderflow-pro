"""
strategies/geometry.py — Barrier-Mathematik fuer Trade-Geometrie.

Portierung von src/core/geometry.ts. Charter §9 fuehrt dieses Modul als
Bestand, es existierte aber nur im TypeScript-Stack.

Charter §8.4 ordnet die Geometrie ausdruecklich hinter das Briefing ein: sie
braucht Barrieren, die der Trader erst im Trade setzt. Hier steht deshalb die
Rechnung, nicht die Empfehlung — evaluate_geometry() beschreibt eine vom
Nutzer gesetzte Geometrie, sie schlaegt keine vor.

BEKANNTE LUECKE (Charter §5): first_passage_gbm() rechnet ueber unendlichen
Horizont. p_timeout ist deshalb konstant 0.0 — das Modell kennt keinen Trade,
der zum Sessionende noch offen ist. Charter §5 verlangt genau diesen Pfad
("bei einer 4-Stunden-Session sind ueber die Haelfte der Trades zum
Sessionende noch offen"). Die Portierung ist bewusst originalgetreu; der
endliche Horizont gehoert in Schritt 4 der Reihenfolge und ist nicht
stillschweigend hier eingebaut.
"""

from dataclasses import dataclass
import math
from typing import List, Literal, Optional

# 1-Minuten-Bars, Krypto 24/7
BARS_PER_YEAR_1M = 525_600

# Unterhalb dieses |lambda| wird der driftfreie Grenzfall gerechnet
_LAMBDA_EPS = 1e-9

# Ab diesem |Drift| wird ueberhaupt nach einem Vorzeichenwechsel gesucht
_DRIFT_EPS = 1e-5


@dataclass(frozen=True)
class TradeGeometry:
    """Ergebnis einer Geometrie-Auswertung. Reine Beschreibung, keine Bewertung."""

    entry: float
    stop: float
    target: float
    spot: float

    rrr: float                  # Reward:Risk in R
    p_target: float             # implizite Trefferquote unter GBM
    p_stop: float
    p_timeout: float            # immer 0.0 — siehe Modul-Docstring

    breakeven_win_rate: float   # Trefferquote, bei der expectancy_r == 0
    expectancy_r: float         # E[R] pro Trade, nach Kosten
    edge_pp: float              # p_target - breakeven in Prozentpunkten

    estimator: Literal["gbm"]
    annual_vol: float
    annual_drift: float
    is_positive: bool
    sign_flip_distance_pct: Optional[float]


def first_passage_gbm(
    spot: float,
    stop: float,
    target: float,
    annual_vol: float,
    annual_drift: float = 0.0,
) -> float:
    """
    P(Target wird vor Stop beruehrt) fuer eine geometrische Brownsche Bewegung
    mit zwei absorbierenden Barrieren.

    Ohne Drift (Martingal) faellt das auf die lineare Ruin-Formel zusammen:
        p_target = ln(spot/stop) / ln(target/stop)

    Charter §2 setzt mu = 0 als Default: ein aus dem Skew abgeleiteter Drift
    lebt unter Q, nicht unter P, und erzeugt Scheinkonvergenz.
    """
    if not (stop < spot < target):
        raise ValueError(
            f"spot muss strikt zwischen stop und target liegen, "
            f"erhalten: stop={stop}, spot={spot}, target={target}"
        )
    if annual_vol <= 0.0:
        raise ValueError(f"annual_vol muss > 0 sein, erhalten: {annual_vol}")

    x = math.log(spot / stop)
    span = math.log(target / stop)
    lam = (2.0 * (annual_drift - 0.5 * annual_vol * annual_vol)) / (annual_vol * annual_vol)

    if abs(lam) < _LAMBDA_EPS:
        return x / span
    return (1.0 - math.exp(-lam * x)) / (1.0 - math.exp(-lam * span))


def realised_vol_annualised(
    returns: List[float],
    bars_per_year: int = BARS_PER_YEAR_1M,
) -> Optional[float]:
    """
    Annualisierte realisierte Volatilitaet aus Log-Returns.

    Gibt None zurueck, wenn zu wenige Datenpunkte vorliegen. Die TypeScript-
    Fassung lieferte hier 0.52 als "standard baseline" — ein Zahlenwert ohne
    Messung, der sich anschliessend nicht mehr von einer echten RV
    unterscheiden liess (Charter §2).
    """
    valid = [r for r in returns if isinstance(r, (int, float)) and math.isfinite(r)]
    if len(valid) < 2:
        return None

    mean = sum(valid) / len(valid)
    variance = sum((r - mean) ** 2 for r in valid) / (len(valid) - 1)
    return math.sqrt(variance) * math.sqrt(bars_per_year)


def evaluate_geometry(
    entry: float,
    stop: float,
    target: float,
    annual_vol: float,
    annual_drift: float = 0.0,
    spot: Optional[float] = None,
    cost_r: float = 0.0,
) -> TradeGeometry:
    """
    Wertet eine vom Trader gesetzte Barrier-Geometrie unter GBM-First-Passage aus.

    Ohne Drift ist E[R] gleichmaessig -cost_r ueber alle gueltigen Entries:
    Die Geometrie allein erzeugt keinen Edge. Genau das soll die Zahl zeigen.
    """
    s = spot if spot is not None else entry

    if not (stop < entry < target):
        raise ValueError(
            f"entry muss strikt zwischen stop und target liegen, "
            f"erhalten: stop={stop}, entry={entry}, target={target}"
        )
    if not (stop < s < target):
        raise ValueError(
            f"spot muss strikt zwischen stop und target liegen, "
            f"erhalten: stop={stop}, spot={s}, target={target}"
        )

    risk = entry - stop
    reward = target - entry
    rrr = reward / risk

    p_target = first_passage_gbm(s, stop, target, annual_vol, annual_drift)
    p_stop = 1.0 - p_target

    expectancy_r = p_target * rrr - p_stop * 1.0 - cost_r
    breakeven = (1.0 + cost_r) / (1.0 + rrr)
    is_positive = expectancy_r > 0.0

    # Abstand bis zum Vorzeichenwechsel von E[R]: existiert nur bei Drift != 0,
    # weil E[R] im driftfreien Fall ueber alle Entries konstant -cost_r ist.
    sign_flip_distance_pct: Optional[float] = None
    if abs(annual_drift) >= _DRIFT_EPS:
        lo = stop * 1.0005
        hi = target * 0.9995
        if lo < hi:
            for i in range(1, 201):
                test_spot = lo + (hi - lo) * i / 200.0
                try:
                    pt = first_passage_gbm(test_spot, stop, target, annual_vol, annual_drift)
                except ValueError:
                    continue
                ev = pt * rrr - (1.0 - pt) - cost_r
                if (ev > 0.0) != is_positive:
                    sign_flip_distance_pct = round(abs(test_spot - s) / s * 100.0, 2)
                    break

    return TradeGeometry(
        entry=entry,
        stop=stop,
        target=target,
        spot=s,
        rrr=round(rrr, 4),
        p_target=round(p_target, 4),
        p_stop=round(p_stop, 4),
        p_timeout=0.0,
        breakeven_win_rate=round(breakeven, 4),
        expectancy_r=round(expectancy_r, 4),
        edge_pp=round((p_target - breakeven) * 100.0, 2),
        estimator="gbm",
        annual_vol=annual_vol,
        annual_drift=annual_drift,
        is_positive=is_positive,
        sign_flip_distance_pct=sign_flip_distance_pct,
    )


def terminal_containment_note() -> str:
    """
    Charter §7.1: 68% ist Terminal-Containment, keine Beruehrungsgrenze.
    Die Wahrscheinlichkeit, das obere 1-Sigma-Band waehrend der Session zu
    beruehren, liegt nach dem Reflexionsprinzip bei rund 32%, nicht bei 68%.
    """
    return (
        "1-Sigma-Band = 68% Terminal-Containment (Wert bei Sessionende). "
        "Beruehrungswahrscheinlichkeit des oberen Bandes waehrend der Session: "
        "rund 32% (Reflexionsprinzip)."
    )
