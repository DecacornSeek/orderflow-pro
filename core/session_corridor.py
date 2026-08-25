"""
core/session_corridor.py — Die vom Optionsmarkt eingepreiste Spanne.

Charter §5: Zeit ist eine erste Dimension, kein Detail. Der Korridor rechnet
mit sqrt(Restzeit bis Reset), nicht mit sqrt(Sessionlaenge). Eine Karte, die
den ganzen Tag dieselbe Spanne zeigt, ist ab Mittag zu weit und laesst Ziele
erreichbar aussehen, die es zeitlich nicht mehr sind.

Charter §7.1: 68% ist Terminal-Containment, keine Beruehrungsgrenze. Die
Wahrscheinlichkeit, das obere 1-Sigma-Band waehrend der Restzeit ueberhaupt
zu beruehren, liegt nach dem Reflexionsprinzip bei rund 32%. Beide Zahlen
werden ausgewiesen, damit die Karte nicht ueberinterpretiert wird.

Reine Funktionen ueber (Spot, IV, Zeit) — keine Zustandshaltung, backtestbar.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import List, Optional

SECONDS_PER_YEAR = 31_536_000.0  # 365 * 86400

# Untergrenze der Restzeit. Ohne sie geht die Spanne in der letzten Minute
# gegen null und die Karte behauptet eine Praezision, die nicht existiert.
MIN_REMAINING_SECONDS = 300.0

# Standard-Sigma-Vielfache der Karte
DEFAULT_SIGMAS = (1.0, 2.0)


@dataclass(frozen=True)
class CorridorBand:
    """Ein Sigma-Band mit beiden Wahrscheinlichkeitslesarten."""

    sigma: float
    low: float
    high: float
    width: float
    # Anteil der Verteilung, der zum Reset INNERHALB des Bandes endet
    terminal_containment: float
    # Wahrscheinlichkeit, das obere Band bis zum Reset mindestens einmal
    # zu beruehren (Reflexionsprinzip, driftfrei): 2 * P(Ende darueber)
    touch_probability_upper: float
    touch_probability_lower: float


@dataclass(frozen=True)
class SessionCorridor:
    """Die eingepreiste Spanne bis zum naechsten Reset."""

    spot: float
    atm_iv: float                    # Dezimalwert, z.B. 0.55
    reset_at: datetime               # UTC
    remaining_seconds: float
    remaining_hours: float
    # sigma in Preiseinheiten ueber die Restzeit: spot * iv * sqrt(T)
    sigma_price: float
    bands: tuple
    # Anteil der Restzeit, der von der vollen Periode noch uebrig ist —
    # macht sichtbar, dass die Spanne im Tagesverlauf schrumpft
    time_decay_factor: float
    floored: bool                    # True wenn MIN_REMAINING_SECONDS gegriffen hat


def _norm_cdf(x: float) -> float:
    """Standardnormale Verteilungsfunktion."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def terminal_containment(sigma: float) -> float:
    """
    Anteil der Verteilung innerhalb von +/- sigma Standardabweichungen
    zum Zeitpunkt des Resets. Fuer sigma = 1 rund 0.6827.
    """
    return 2.0 * _norm_cdf(sigma) - 1.0


def touch_probability(sigma: float) -> float:
    """
    Wahrscheinlichkeit, eine Barriere bei +sigma waehrend der Restzeit
    mindestens einmal zu beruehren.

    Reflexionsprinzip fuer die driftfreie Brownsche Bewegung:
        P(max_[0,T] > b) = 2 * P(W_T > b)

    Fuer sigma = 1 sind das rund 0.3173 — also ungefaehr ein Drittel, nicht
    die 32% Gegenwahrscheinlichkeit zu 68%. Charter §7.1 haengt genau hier.
    """
    return 2.0 * (1.0 - _norm_cdf(sigma))


def seconds_until(now_utc: datetime, target_utc: datetime) -> float:
    """Restsekunden bis zum Ziel; nie negativ."""
    return max(0.0, (target_utc - now_utc).total_seconds())


def next_daily_settlement(now_utc: datetime, hour: int = 8) -> datetime:
    """
    Naechstes taegliches Settlement (Deribit: 08:00 UTC).

    Das ist der natuerliche Reset fuer einen Korridor aus 0DTE-IV: die
    0DTE-Kette selbst verfaellt zu diesem Zeitpunkt.
    """
    candidate = now_utc.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now_utc:
        candidate += timedelta(days=1)
    return candidate


def build_corridor(
    spot: float,
    atm_iv: Optional[float],
    now_utc: datetime,
    reset_at: Optional[datetime] = None,
    sigmas: tuple = DEFAULT_SIGMAS,
    full_period_seconds: float = 86400.0,
) -> Optional[SessionCorridor]:
    """
    Baut den Korridor bis zum naechsten Reset.

    Gibt None zurueck, wenn Spot oder IV fehlen — es wird keine Spanne
    geschaetzt (Charter §2). Ohne gemessene IV gibt es keine eingepreiste
    Spanne, nur eine erfundene.

    Args:
        spot: aktueller Preis
        atm_iv: ATM Mark-IV als Dezimalwert (0.55 = 55%); None -> kein Korridor
        now_utc: Bezugszeitpunkt
        reset_at: Zielzeitpunkt; Default ist das naechste 08:00-UTC-Settlement
        sigmas: gewuenschte Vielfache
        full_period_seconds: Bezugsperiode fuer time_decay_factor (Default 24h)
    """
    if spot is None or spot <= 0.0:
        return None
    if atm_iv is None or atm_iv <= 0.0 or math.isnan(atm_iv):
        return None

    if reset_at is None:
        reset_at = next_daily_settlement(now_utc)

    raw_remaining = seconds_until(now_utc, reset_at)
    floored = raw_remaining < MIN_REMAINING_SECONDS
    remaining = max(raw_remaining, MIN_REMAINING_SECONDS)

    t_years = remaining / SECONDS_PER_YEAR
    sigma_price = spot * atm_iv * math.sqrt(t_years)

    bands = tuple(
        CorridorBand(
            sigma=k,
            low=spot - k * sigma_price,
            high=spot + k * sigma_price,
            width=2.0 * k * sigma_price,
            terminal_containment=terminal_containment(k),
            touch_probability_upper=touch_probability(k),
            touch_probability_lower=touch_probability(k),
        )
        for k in sigmas
    )

    return SessionCorridor(
        spot=spot,
        atm_iv=atm_iv,
        reset_at=reset_at,
        remaining_seconds=raw_remaining,
        remaining_hours=raw_remaining / 3600.0,
        sigma_price=sigma_price,
        bands=bands,
        time_decay_factor=math.sqrt(remaining / full_period_seconds) if full_period_seconds > 0 else 1.0,
        floored=floored,
    )


def target_position(corridor: Optional[SessionCorridor], target: float) -> Optional[dict]:
    """
    Ordnet ein vom Trader gesetztes Target in den Korridor ein.

    Das ist der eigentliche Zweck der Karte: liegt das Target innerhalb der
    eingepreisten Verteilung, und reicht die Restzeit dafuer? Beschreibung,
    keine Bewertung — es steht kein "gut" oder "schlecht" dabei.
    """
    if corridor is None or target is None or corridor.sigma_price <= 0.0:
        return None

    distance = target - corridor.spot
    sigma_distance = abs(distance) / corridor.sigma_price

    return {
        "target": target,
        "distance": distance,
        "distance_pct": distance / corridor.spot * 100.0,
        "sigma_distance": sigma_distance,
        "direction": "above" if distance > 0 else ("below" if distance < 0 else "at_spot"),
        # Wahrscheinlichkeit, das Target bis zum Reset zu beruehren
        "touch_probability": touch_probability(sigma_distance),
        # Innerhalb welcher Baender das Target liegt
        "inside_bands": [b.sigma for b in corridor.bands if b.low <= target <= b.high],
        "remaining_hours": corridor.remaining_hours,
    }


def corridor_to_dict(corridor: Optional[SessionCorridor]) -> Optional[dict]:
    """Serialisierung fuer die HTTP-Schicht."""
    if corridor is None:
        return None
    return {
        "spot": corridor.spot,
        "atm_iv": corridor.atm_iv,
        "reset_at": corridor.reset_at.isoformat(),
        "remaining_seconds": round(corridor.remaining_seconds, 1),
        "remaining_hours": round(corridor.remaining_hours, 2),
        "sigma_price": corridor.sigma_price,
        "time_decay_factor": round(corridor.time_decay_factor, 4),
        "floored": corridor.floored,
        "bands": [
            {
                "sigma": b.sigma,
                "low": b.low,
                "high": b.high,
                "width": b.width,
                "terminal_containment": round(b.terminal_containment, 4),
                "touch_probability_upper": round(b.touch_probability_upper, 4),
                "touch_probability_lower": round(b.touch_probability_lower, 4),
            }
            for b in corridor.bands
        ],
        "caveat": (
            "Terminal-Containment ist der Anteil, der zum Reset im Band endet. "
            "Die Wahrscheinlichkeit, das Band zwischendurch zu beruehren, ist "
            "rund doppelt so hoch wie die, ausserhalb zu enden."
        ),
    }
