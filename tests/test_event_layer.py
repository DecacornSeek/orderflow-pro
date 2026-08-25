"""
tests/test_event_layer.py — Zeitraster der Session (Charter §4.1).

Deterministischer Kalender, keine externe Quelle. Die Tests halten vor allem
die Grenzfaelle fest, an denen Datumsrechnung schiefgeht: Monatswechsel,
Jahreswechsel und die Sommerzeit im Prop-Reset.
"""

from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.event_layer import (  # noqa: E402
    KIND_EXPIRY,
    KIND_FUNDING,
    KIND_MACRO,
    KIND_PROP,
    build_events,
    events_to_dict,
    last_friday_of_month,
    next_event_of,
    next_fundingpips_reset,
    next_monthly_expiry,
)


def _utc(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ── Monatsverfall: letzter Freitag ────────────────────────────────────────────

@pytest.mark.parametrize("year,month,erwartet", [
    (2026, 8, (2026, 8, 28)),
    (2026, 12, (2026, 12, 25)),
    (2026, 1, (2026, 1, 30)),
    (2027, 2, (2027, 2, 26)),
])
def test_letzter_freitag_des_monats(year, month, erwartet):
    dt = last_friday_of_month(year, month)
    assert (dt.year, dt.month, dt.day) == erwartet
    assert dt.weekday() == 4          # Freitag
    assert (dt.hour, dt.minute) == (8, 0)
    assert dt.tzinfo == timezone.utc


def test_monatsverfall_rollt_in_den_folgemonat():
    """Ist der letzte Freitag vorbei, zaehlt der des naechsten Monats."""
    nach_verfall = _utc(2026, 8, 28, 9)     # kurz nach dem August-Verfall
    assert next_monthly_expiry(nach_verfall) == last_friday_of_month(2026, 9)


def test_monatsverfall_rollt_ueber_den_jahreswechsel():
    nach_dezember = _utc(2026, 12, 26, 9)
    assert next_monthly_expiry(nach_dezember) == last_friday_of_month(2027, 1)


# ── Prop-Reset mit Sommerzeit ─────────────────────────────────────────────────

def test_fundingpips_reset_folgt_der_sommerzeit():
    """
    17:00 Ortszeit New York bedeutet 21:00 UTC im Sommer und 22:00 UTC im
    Winter. Ein fester UTC-Versatz waere an einem der beiden Termine falsch.
    """
    sommer = next_fundingpips_reset(_utc(2026, 8, 25, 9))
    winter = next_fundingpips_reset(_utc(2026, 1, 15, 9))

    assert sommer.hour == 21
    assert winter.hour == 22


def test_fundingpips_reset_liegt_immer_in_der_zukunft():
    for stunde in (0, 6, 12, 18, 21, 22, 23):
        now = _utc(2026, 8, 25, stunde)
        assert next_fundingpips_reset(now) > now


# ── Ereignisliste ─────────────────────────────────────────────────────────────

def test_ereignisse_sind_nach_restzeit_sortiert():
    events = build_events(_utc(2026, 8, 25, 9, 15))
    zeiten = [e.seconds_until for e in events]
    assert zeiten == sorted(zeiten)
    assert all(e.seconds_until >= 0 for e in events)


def test_alle_kategorien_kommen_vor():
    events = build_events(_utc(2026, 8, 25, 9, 15))
    kinds = {e.kind for e in events}
    assert {KIND_EXPIRY, KIND_FUNDING, KIND_MACRO, KIND_PROP} <= kinds


def test_beide_prop_resets_sind_enthalten():
    events = build_events(_utc(2026, 8, 25, 9, 15))
    labels = {e.label for e in events if e.kind == KIND_PROP}
    assert "Breakout Daily Reset" in labels
    assert "FundingPips Daily Reset" in labels


def test_horizont_begrenzt_die_liste():
    """Der Monatsverfall faellt heraus, solange er weit weg ist."""
    kurz = build_events(_utc(2026, 8, 3, 9), horizon_hours=6.0)
    lang = build_events(_utc(2026, 8, 3, 9), horizon_hours=720.0)

    assert all(e.hours_until <= 6.0 for e in kurz)
    assert len(lang) > len(kurz)
    assert any("Monthly" in e.label for e in lang)
    assert not any("Monthly" in e.label for e in kurz)


def test_naechster_verfall_ist_das_tagessettlement():
    """Um 09:15 UTC ist der naechste Verfall der von morgen 08:00."""
    events = build_events(_utc(2026, 8, 25, 9, 15))
    verfall = next_event_of(events, KIND_EXPIRY)
    assert verfall.label == "Deribit Daily Expiry"
    assert verfall.at_utc == _utc(2026, 8, 26, 8)


def test_makro_fenster_ist_als_fenster_gekennzeichnet():
    """
    Ohne Wirtschaftskalender darf kein konkreter Termin behauptet werden —
    der Eintrag muss sich selbst als blosses Zeitfenster ausweisen.
    """
    events = build_events(_utc(2026, 8, 25, 9, 15))
    makro = next_event_of(events, KIND_MACRO)
    assert "Slot" in makro.label
    assert "KEINE bestaetigte" in makro.note


def test_serialisierung():
    events = build_events(_utc(2026, 8, 25, 9, 15))
    payload = events_to_dict(events)
    assert len(payload) == len(events)
    assert all({"kind", "label", "at_utc", "seconds_until", "hours_until", "note"} <= set(e)
               for e in payload)


def test_ereignisse_bleiben_ueber_die_tagesgrenze_konsistent():
    """Kurz vor Mitternacht darf nichts in die Vergangenheit rutschen."""
    now = _utc(2026, 8, 25, 23, 55)
    for e in build_events(now):
        assert e.at_utc > now


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
