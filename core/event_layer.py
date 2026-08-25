"""
core/event_layer.py — Der Zeitraster, gegen den die Session laeuft.

Charter §4.1: Event-Layer mit 08:00 UTC Daily, Freitag Weekly, Monthly
(letzter Freitag), Funding-Resets 00/08/16 UTC, Makro-Fenster und den
Prop-Resets des Traders (Breakout 00:30 UTC, FundingPips 17:00 New York).

Alles deterministisch aus dem Kalender gerechnet — keine externe Quelle, kein
Netzzugriff. Was hier steht, sind wiederkehrende Termine, keine Vorhersagen.

Bewusste Grenze: Konkrete Makro-Termine (CPI, FOMC, NFP) brauchen einen
Wirtschaftskalender, den es hier nicht gibt. Ausgewiesen wird deshalb nur das
wiederkehrende US-Datenfenster als Zeitfenster, ausdruecklich nicht als
bestaetigte Veroeffentlichung.
"""

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")

# Kategorien — die Karte faerbt danach, nicht nach Wichtigkeit
KIND_EXPIRY = "expiry"
KIND_FUNDING = "funding"
KIND_MACRO = "macro"
KIND_PROP = "prop_reset"

DERIBIT_SETTLEMENT_HOUR = 8      # 08:00 UTC
FUNDING_HOURS = (0, 8, 16)       # UTC
MACRO_WINDOW_UTC = dtime(13, 30)  # US-Datenfenster (8:30 ET Standardzeit-Slot)
BREAKOUT_RESET_UTC = dtime(0, 30)
FUNDINGPIPS_RESET_LOCAL = dtime(17, 0)  # 17:00 New York


@dataclass(frozen=True)
class MarketEvent:
    """Ein anstehender Termin mit Restzeit."""

    kind: str
    label: str
    at_utc: datetime
    seconds_until: float
    # Erlaeuterung, warum der Termin fuer die Positionierung zaehlt
    note: str = ""

    @property
    def hours_until(self) -> float:
        return self.seconds_until / 3600.0


def _next_daily(now: datetime, hour: int, minute: int = 0) -> datetime:
    """Naechstes Auftreten einer taeglichen UTC-Uhrzeit."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _next_weekday(now: datetime, weekday: int, hour: int, minute: int = 0) -> datetime:
    """Naechstes Auftreten eines Wochentags (0 = Montag) zur UTC-Uhrzeit."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (weekday - candidate.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def last_friday_of_month(year: int, month: int) -> datetime:
    """Letzter Freitag eines Monats, 08:00 UTC — der Deribit-Monatsverfall."""
    if month == 12:
        first_next = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        first_next = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    last_day = first_next - timedelta(days=1)
    # 4 = Freitag
    offset = (last_day.weekday() - 4) % 7
    friday = last_day - timedelta(days=offset)
    return friday.replace(hour=DERIBIT_SETTLEMENT_HOUR, minute=0, second=0, microsecond=0)


def next_monthly_expiry(now: datetime) -> datetime:
    """Naechster Monatsverfall, ggf. schon im Folgemonat."""
    this_month = last_friday_of_month(now.year, now.month)
    if this_month > now:
        return this_month
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return last_friday_of_month(year, month)


def next_fundingpips_reset(now: datetime) -> datetime:
    """
    Naechster FundingPips-Reset: 17:00 Ortszeit New York.

    Bewusst als Ortszeit gerechnet, nicht als fester UTC-Versatz: Prop-Firmen
    meinen in aller Regel die lokale Uhr, die der Sommerzeit folgt. Damit
    verschiebt sich der Reset zwischen 21:00 und 22:00 UTC. Sollte die Firma
    tatsaechlich fixes UTC-5 ganzjaehrig meinen, gehoert hier eine feste
    Zeitzone hin.
    """
    local_now = now.astimezone(NEW_YORK)
    candidate = local_now.replace(
        hour=FUNDINGPIPS_RESET_LOCAL.hour,
        minute=FUNDINGPIPS_RESET_LOCAL.minute,
        second=0,
        microsecond=0,
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def build_events(now_utc: Optional[datetime] = None, horizon_hours: float = 48.0) -> List[MarketEvent]:
    """
    Alle anstehenden Termine innerhalb des Horizonts, nach Restzeit sortiert.

    Der Horizont haelt die Liste kurz genug fuer eine scrollfreie Karte;
    Monatsverfall und Weekly ueberschreiten ihn regelmaessig und fallen dann
    heraus — sie stehen in der Liste erst, wenn sie relevant werden.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    candidates: List[MarketEvent] = []

    def add(kind: str, label: str, at: datetime, note: str = "") -> None:
        delta = (at - now_utc).total_seconds()
        if 0 <= delta <= horizon_hours * 3600.0:
            candidates.append(MarketEvent(kind, label, at, delta, note))

    # Taeglicher Deribit-Verfall
    add(KIND_EXPIRY, "Deribit Daily Expiry",
        _next_daily(now_utc, DERIBIT_SETTLEMENT_HOUR),
        "0DTE-Kette verfaellt; Gamma des Tages faellt aus dem Buch.")

    # Weekly: Freitag 08:00 UTC
    add(KIND_EXPIRY, "Deribit Weekly Expiry",
        _next_weekday(now_utc, 4, DERIBIT_SETTLEMENT_HOUR),
        "Woechentliche Kette; deutlich groesseres OI als Daily.")

    # Monthly: letzter Freitag
    add(KIND_EXPIRY, "Deribit Monthly Expiry",
        next_monthly_expiry(now_utc),
        "Groesstes OI-Buendel des Monats.")

    # Funding-Resets
    for hour in FUNDING_HOURS:
        add(KIND_FUNDING, f"Funding Reset {hour:02d}:00 UTC",
            _next_daily(now_utc, hour),
            "Perp-Funding wird verrechnet; Positionierung kann kippen.")

    # US-Datenfenster — Fenster, nicht Termin
    add(KIND_MACRO, "US-Datenfenster (Slot)",
        _next_daily(now_utc, MACRO_WINDOW_UTC.hour, MACRO_WINDOW_UTC.minute),
        "Wiederkehrendes Zeitfenster, KEINE bestaetigte Veroeffentlichung — "
        "konkrete Termine brauchen einen Wirtschaftskalender.")

    # Prop-Resets des Traders
    add(KIND_PROP, "Breakout Daily Reset",
        _next_daily(now_utc, BREAKOUT_RESET_UTC.hour, BREAKOUT_RESET_UTC.minute),
        "Daily-Loss-Limit setzt zurueck. Hartes Limit, kein weicher Stop.")

    add(KIND_PROP, "FundingPips Daily Reset",
        next_fundingpips_reset(now_utc),
        "17:00 Ortszeit New York; folgt der Sommerzeit.")

    return sorted(candidates, key=lambda e: e.seconds_until)


def next_event_of(events: List[MarketEvent], kind: str) -> Optional[MarketEvent]:
    """Naechster Termin einer Kategorie."""
    return next((e for e in events if e.kind == kind), None)


def events_to_dict(events: List[MarketEvent]) -> List[dict]:
    """Serialisierung fuer die HTTP-Schicht."""
    return [
        {
            "kind": e.kind,
            "label": e.label,
            "at_utc": e.at_utc.isoformat(),
            "seconds_until": round(e.seconds_until, 0),
            "hours_until": round(e.hours_until, 2),
            "note": e.note,
        }
        for e in events
    ]
