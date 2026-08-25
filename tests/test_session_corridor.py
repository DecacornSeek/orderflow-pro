"""
tests/test_session_corridor.py — Die eingepreiste Spanne (Charter §5, §7.1).

Kern: der Korridor muss ueber den Tag schrumpfen, und die beiden
Wahrscheinlichkeitslesarten duerfen nicht verwechselt werden.
"""

from datetime import datetime, timedelta, timezone
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.session_corridor import (  # noqa: E402
    MIN_REMAINING_SECONDS,
    build_corridor,
    corridor_to_dict,
    next_daily_settlement,
    target_position,
    terminal_containment,
    touch_probability,
)

SPOT = 100000.0
IV = 0.55


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 25, hour, minute, tzinfo=timezone.utc)


# ── Zeit ist eine erste Dimension (§5) ────────────────────────────────────────

def test_korridor_schrumpft_ueber_den_tag():
    """
    Die zentrale Zusage aus §5: dieselbe IV, spaeterer Zeitpunkt, engere
    Spanne. Eine Karte mit konstanter Spanne waere ab Mittag zu weit.
    """
    frueh = build_corridor(SPOT, IV, _at(9))
    spaet = build_corridor(SPOT, IV, _at(20))

    assert frueh is not None and spaet is not None
    assert spaet.remaining_seconds < frueh.remaining_seconds
    assert spaet.sigma_price < frueh.sigma_price
    assert spaet.bands[0].width < frueh.bands[0].width


def test_sigma_skaliert_mit_wurzel_der_restzeit():
    """Vierfache Restzeit muss die Spanne genau verdoppeln, nicht vervierfachen."""
    reset = _at(9) + timedelta(hours=8)
    kurz = build_corridor(SPOT, IV, _at(9), reset_at=_at(9) + timedelta(hours=2))
    lang = build_corridor(SPOT, IV, _at(9), reset_at=_at(9) + timedelta(hours=8))

    assert lang.sigma_price == pytest.approx(2.0 * kurz.sigma_price, rel=1e-9)
    assert reset == lang.reset_at


def test_reset_ist_das_naechste_0800_settlement():
    assert next_daily_settlement(_at(7)) == _at(8)
    assert next_daily_settlement(_at(9)) == _at(8) + timedelta(days=1)
    # Exakt auf der Grenze zaehlt der naechste Tag
    assert next_daily_settlement(_at(8)) == _at(8) + timedelta(days=1)


def test_restzeit_hat_eine_untergrenze():
    """
    Kurz vor Settlement geht die Spanne sonst gegen null und die Karte
    behauptet eine Praezision, die es nicht gibt.
    """
    reset = _at(8)
    c = build_corridor(SPOT, IV, reset - timedelta(seconds=30), reset_at=reset)

    assert c.floored is True
    assert c.remaining_seconds == pytest.approx(30.0)
    assert c.sigma_price > 0.0
    # Gerechnet wird mit dem Floor, nicht mit 30 Sekunden
    erwartet = SPOT * IV * math.sqrt(MIN_REMAINING_SECONDS / 31_536_000.0)
    assert c.sigma_price == pytest.approx(erwartet)


# ── Die beiden Wahrscheinlichkeiten (§7.1) ────────────────────────────────────

def test_terminal_containment_gegen_beruehrung():
    """
    68% ist Terminal-Containment. Die Beruehrungswahrscheinlichkeit des
    oberen Bandes liegt bei rund 32%, nicht bei 32% Gegenwahrscheinlichkeit
    zu 68% — beide Zahlen muessen getrennt herauskommen.
    """
    assert terminal_containment(1.0) == pytest.approx(0.6827, abs=1e-4)
    assert touch_probability(1.0) == pytest.approx(0.3173, abs=1e-4)
    assert terminal_containment(2.0) == pytest.approx(0.9545, abs=1e-4)
    assert touch_probability(2.0) == pytest.approx(0.0455, abs=1e-4)


def test_beruehrung_ist_doppelt_so_wahrscheinlich_wie_ausserhalb_enden():
    """Reflexionsprinzip: P(beruehrt) = 2 * P(endet darueber)."""
    for sigma in (0.5, 1.0, 1.5, 2.0, 3.0):
        endet_darueber = (1.0 - terminal_containment(sigma)) / 2.0
        assert touch_probability(sigma) == pytest.approx(2.0 * endet_darueber, rel=1e-9)


# ── Einordnung eines Targets — der eigentliche Zweck der Karte ────────────────

def test_target_innerhalb_und_ausserhalb_der_spanne():
    c = build_corridor(SPOT, IV, _at(9))
    eng = target_position(c, SPOT + 0.5 * c.sigma_price)
    weit = target_position(c, SPOT + 3.0 * c.sigma_price)

    assert eng["sigma_distance"] == pytest.approx(0.5, rel=1e-6)
    assert 1.0 in eng["inside_bands"] and 2.0 in eng["inside_bands"]
    assert eng["direction"] == "above"

    assert weit["inside_bands"] == []
    assert weit["touch_probability"] < eng["touch_probability"]


def test_target_unterhalb_des_spot():
    c = build_corridor(SPOT, IV, _at(9))
    unten = target_position(c, SPOT - c.sigma_price)

    assert unten["direction"] == "below"
    assert unten["distance"] < 0
    assert unten["sigma_distance"] == pytest.approx(1.0, rel=1e-6)
    assert unten["touch_probability"] == pytest.approx(touch_probability(1.0), rel=1e-6)


# ── Kein Korridor ohne Datengrundlage (§2) ────────────────────────────────────

@pytest.mark.parametrize("spot,iv", [
    (None, IV),
    (0.0, IV),
    (SPOT, None),
    (SPOT, 0.0),
    (SPOT, float("nan")),
])
def test_ohne_messwerte_kein_korridor(spot, iv):
    assert build_corridor(spot, iv, _at(9)) is None


def test_target_ohne_korridor_ist_none():
    assert target_position(None, 102000.0) is None


def test_serialisierung_traegt_den_caveat():
    d = corridor_to_dict(build_corridor(SPOT, IV, _at(9)))
    assert d["bands"][0]["terminal_containment"] == pytest.approx(0.6827, abs=1e-4)
    assert d["bands"][0]["touch_probability_upper"] == pytest.approx(0.3173, abs=1e-4)
    assert "Terminal-Containment" in d["caveat"]
    assert corridor_to_dict(None) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
