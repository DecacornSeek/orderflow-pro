"""
tests/test_regime_state.py — Stabilitaet der Anzeige (Charter §6).

Eine flackernde Anzeige ist schlechter als keine. Die Tests halten fest, dass
kurze Ausreisser nicht durchschlagen, die Rueckkehr in die Unsicherheit aber
sofort erlaubt ist.
"""

from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.regime_state import (  # noqa: E402
    LONG_GAMMA,
    SHORT_GAMMA,
    TRANSITION,
    UNKNOWN,
    ChangeLog,
    RegimeTracker,
    SessionAnchor,
)

T0 = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)


def _t(sekunden: float) -> datetime:
    return T0 + timedelta(seconds=sekunden)


def _eingespielt(tracker: RegimeTracker, wert: float = 5_000_000.0) -> None:
    """Referenzgroesse aufbauen und den Zustand festsetzen."""
    for i in range(5):
        tracker.update(wert, _t(i * 40))


# ── Hysterese ─────────────────────────────────────────────────────────────────

def test_umschaltung_verlangt_verweildauer():
    tr = RegimeTracker(dwell_seconds=120.0)
    _eingespielt(tr)
    assert tr.update(5_000_000.0, _t(200)).state == LONG_GAMMA

    # Vorzeichenwechsel: nicht sofort
    s = tr.update(-6_000_000.0, _t(210))
    assert s.state == LONG_GAMMA
    assert s.pending_state == SHORT_GAMMA
    assert s.raw_state == SHORT_GAMMA      # der Rohwert sagt bereits short

    s = tr.update(-6_000_000.0, _t(300))
    assert s.state == LONG_GAMMA           # 90s, noch nicht genug

    s = tr.update(-6_000_000.0, _t(340))
    assert s.state == SHORT_GAMMA          # 130s, umgeschaltet
    assert s.changed_at == _t(340)


def test_kurzer_ausreisser_schaltet_nicht_um():
    """Der Fall, der die Anzeige sonst am Flip flackern laesst."""
    tr = RegimeTracker(dwell_seconds=120.0)
    _eingespielt(tr)
    tr.update(5_000_000.0, _t(200))

    tr.update(-6_000_000.0, _t(210))
    tr.update(-6_000_000.0, _t(240))       # 30s jenseits des Bandes
    s = tr.update(5_000_000.0, _t(270))    # zurueck

    assert s.state == LONG_GAMMA
    assert s.pending_state is None         # Kandidat verworfen


def test_totband_wird_sofort_eingeraeumt():
    """
    Unsicherheit einzuraeumen braucht keine Wartezeit, eine Behauptung schon.
    """
    tr = RegimeTracker(dwell_seconds=120.0)
    _eingespielt(tr)
    assert tr.update(5_000_000.0, _t(200)).state == LONG_GAMMA

    s = tr.update(50_000.0, _t(210))       # innerhalb des Totbands
    assert s.state == TRANSITION
    assert s.raw_state == TRANSITION
    assert s.changed_at == _t(210)


def test_totband_skaliert_mit_der_beobachteten_groessenordnung():
    klein = RegimeTracker(band_pct=0.10)
    gross = RegimeTracker(band_pct=0.10)
    for i in range(10):
        klein.update(1_000_000.0, _t(i))
        gross.update(50_000_000.0, _t(i))

    assert klein.update(1_000_000.0, _t(20)).band == pytest.approx(100_000.0)
    assert gross.update(50_000_000.0, _t(20)).band == pytest.approx(5_000_000.0)


def test_erste_festlegung_verlangt_ebenfalls_verweildauer():
    """Beim Start wird kein Regime behauptet, bevor es gehalten hat."""
    tr = RegimeTracker(dwell_seconds=120.0)
    assert tr.update(5_000_000.0, _t(0)).state == UNKNOWN
    assert tr.update(5_000_000.0, _t(60)).state == UNKNOWN
    assert tr.update(5_000_000.0, _t(130)).state == LONG_GAMMA


def test_fehlender_wert_haelt_den_zustand():
    """Kein Wert heisst nicht ausgeglichen — der Zustand bleibt stehen."""
    tr = RegimeTracker(dwell_seconds=120.0)
    _eingespielt(tr)
    tr.update(5_000_000.0, _t(200))

    s = tr.update(None, _t(210))
    assert s.state == LONG_GAMMA
    assert s.raw_state == UNKNOWN
    assert s.net_gex is None


# ── Session-Anker ─────────────────────────────────────────────────────────────

def test_anker_wird_beim_ersten_wert_gesetzt():
    anker = SessionAnchor(boundary_hour=8)
    anker.update({"spot": 100000.0, "zero_gamma": 100500.0}, _t(0))

    d = anker.to_dict({"spot": 100800.0, "zero_gamma": 100200.0})
    assert d["values"]["spot"] == 100000.0
    assert d["deltas"]["spot"] == pytest.approx(800.0)
    assert d["deltas"]["zero_gamma"] == pytest.approx(-300.0)


def test_anker_haelt_innerhalb_der_session():
    """Spaetere Werte duerfen den Anker nicht ueberschreiben."""
    anker = SessionAnchor(boundary_hour=8)
    anker.update({"spot": 100000.0}, _t(0))
    anker.update({"spot": 101000.0}, _t(3600))

    assert anker.to_dict({"spot": 101000.0})["values"]["spot"] == 100000.0


def test_anker_wird_an_der_sessiongrenze_neu_gesetzt():
    """Nach 08:00 UTC beginnt die naechste Session mit eigenem Anker."""
    anker = SessionAnchor(boundary_hour=8)
    anker.update({"spot": 100000.0}, datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc))
    anker.update({"spot": 97000.0}, datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc))

    assert anker.to_dict({"spot": 97000.0})["values"]["spot"] == 97000.0


def test_fehlende_werte_erzeugen_kein_delta():
    anker = SessionAnchor()
    anker.update({"spot": 100000.0, "zero_gamma": None}, _t(0))

    d = anker.to_dict({"spot": 100500.0, "zero_gamma": 100200.0})
    assert d["deltas"]["spot"] == pytest.approx(500.0)
    assert d["deltas"]["zero_gamma"] is None      # kein Ankerwert vorhanden


# ── Aenderungsliste ───────────────────────────────────────────────────────────

def test_wiederholung_wird_entprellt():
    log = ChangeLog()
    assert log.record("regime", "LONG_GAMMA -> SHORT_GAMMA", _t(0)) is True
    assert log.record("regime", "LONG_GAMMA -> SHORT_GAMMA", _t(10)) is False
    assert len(log.entries()) == 1


def test_neue_aussage_wird_aufgenommen():
    log = ChangeLog()
    log.record("regime", "LONG_GAMMA -> SHORT_GAMMA", _t(0))
    assert log.record("regime", "SHORT_GAMMA -> TRANSITION", _t(10)) is True
    assert len(log.entries()) == 2
    assert log.entries()[0]["text"] == "SHORT_GAMMA -> TRANSITION"   # neueste zuerst


def test_kategorien_entprellen_unabhaengig():
    log = ChangeLog()
    log.record("regime", "gleich", _t(0))
    assert log.record("wall", "gleich", _t(1)) is True


def test_liste_ist_begrenzt():
    log = ChangeLog(maxlen=5)
    for i in range(20):
        log.record("test", f"Aenderung {i}", _t(i))
    assert len(log.entries()) == 5


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
