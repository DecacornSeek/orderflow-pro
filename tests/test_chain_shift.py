"""
tests/test_chain_shift.py — Zwei-Uhren-Zerlegung (Charter §4.2).

Die zentrale Designentscheidung des Systems: eine Level-Verschiebung, die vom
Spot kommt, ist Rauschen; eine bei stehendem Spot ist das Signal. Werden beide
gleich dargestellt, ist der zweite Fall unsichtbar.

Diese Tests halten fest, dass die Zerlegung das auch wirklich trennt — und
zwar exakt, nicht ungefaehr: bewegt sich nur der Spot, muss der informative
Anteil null sein.
"""

from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.options_agent import HistoryManager, OptionsAgent  # noqa: E402
from core.broker import Broker  # noqa: E402


def _expiry_code(dt: datetime) -> str:
    return f"{dt.day}{dt.strftime('%b').upper()}{dt.strftime('%y')}"


def _tomorrow_0800() -> datetime:
    now = datetime.now(timezone.utc)
    exp = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if exp <= now:
        exp += timedelta(days=1)
    return exp


def _chain(oi_map: dict, underlying: float = 100000.0) -> list:
    """Kette aus {(strike, typ): open_interest}."""
    code = _expiry_code(_tomorrow_0800())
    return [
        {
            "instrument_name": f"BTC-{code}-{strike}-{opt_type}",
            "open_interest": oi,
            "mark_iv": 55.0,
            "underlying_price": underlying,
        }
        for (strike, opt_type), oi in oi_map.items()
    ]


# Call-Seite ueberwiegt bewusst: nur dann kreuzt das kumulierte GEX ueberhaupt
# durch null und es gibt einen Zero-Gamma-Level, dessen Verschiebung sich
# messen laesst. Ohne Nulldurchgang ist zero_gamma None — korrekt, aber als
# Fixture fuer diese Tests unbrauchbar.
BASE_OI = {
    (97000, "P"): 120.0,
    (98000, "P"): 160.0,
    (99000, "P"): 200.0,
    (101000, "C"): 600.0,
    (102000, "C"): 420.0,
    (103000, "C"): 260.0,
}


def _agent(tmp_path, spot: float = 100000.0) -> OptionsAgent:
    agent = OptionsAgent(Broker(), history_manager=HistoryManager(tmp_path / "h.json"))
    agent._current_spot = spot
    agent._spot_at_chain = spot
    agent._raw_chain = _chain(BASE_OI)
    agent._stale = False
    return agent


# ── Reiner Spot-Move: informativ muss exakt null sein ─────────────────────────

def test_reiner_spot_move_ist_vollstaendig_mechanisch(tmp_path):
    """
    Gleiche Kette, neuer Spot. Zero-Gamma verschiebt sich — aber es wurde kein
    Open Interest bewegt, also darf nichts als informativ durchkommen.
    """
    agent = _agent(tmp_path)
    now = datetime.now(timezone.utc)

    agent._prev_chain = _chain(BASE_OI)      # identische Kette
    agent._current_spot = 100800.0           # Spot ist gelaufen

    shifts = agent.compute_chain_shift(now)
    assert shifts, "0DTE-Gruppe muss vergleichbar sein"

    zero_dte = next(s for s in shifts if s.label == "0dte")
    assert zero_dte.spot_move == pytest.approx(800.0)
    assert zero_dte.zero_gamma_informative == pytest.approx(0.0)
    assert zero_dte.net_gex_informative == pytest.approx(0.0)
    assert zero_dte.oi_delta_btc == pytest.approx(0.0)
    assert zero_dte.put_wall_moved is False
    assert zero_dte.call_wall_moved is False
    assert zero_dte.is_informative is False


def test_spot_move_erzeugt_mechanische_verschiebung(tmp_path):
    """Gegenprobe: der mechanische Anteil darf nicht ebenfalls null sein."""
    agent = _agent(tmp_path)
    agent._prev_chain = _chain(BASE_OI)
    agent._current_spot = 100800.0

    zero_dte = next(s for s in agent.compute_chain_shift(datetime.now(timezone.utc))
                    if s.label == "0dte")
    assert zero_dte.zero_gamma_mechanical is not None
    assert zero_dte.zero_gamma_mechanical != 0.0


# ── Reiner OI-Aufbau bei stehendem Spot: das ist das Signal ───────────────────

def test_oi_aufbau_bei_stehendem_spot_ist_informativ(tmp_path):
    """
    Spot steht, aber es wurde Open Interest aufgebaut. Genau der Fall, in dem
    jemand gezwungen positioniert — er muss als informativ herauskommen.
    """
    agent = _agent(tmp_path)
    agent._prev_chain = _chain(BASE_OI)

    neue_oi = dict(BASE_OI)
    neue_oi[(101000, "C")] = 1400.0  # massiver Call-Aufbau
    agent._raw_chain = _chain(neue_oi)
    # Spot unveraendert

    zero_dte = next(s for s in agent.compute_chain_shift(datetime.now(timezone.utc))
                    if s.label == "0dte")

    assert zero_dte.spot_move == pytest.approx(0.0)
    assert zero_dte.zero_gamma_mechanical == pytest.approx(0.0)
    assert zero_dte.net_gex_informative > 0.0
    assert zero_dte.is_informative is True


def test_wall_wanderung_wird_als_informativ_erkannt(tmp_path):
    """Wandert eine Wall bei stehendem Spot, ist das eine OI-Aussage."""
    agent = _agent(tmp_path)
    agent._prev_chain = _chain(BASE_OI)

    neue_oi = dict(BASE_OI)
    neue_oi[(103000, "C")] = 5000.0  # Call-Wall wandert von 101k auf 103k
    agent._raw_chain = _chain(neue_oi)

    zero_dte = next(s for s in agent.compute_chain_shift(datetime.now(timezone.utc))
                    if s.label == "0dte")
    assert zero_dte.call_wall_moved is True
    assert zero_dte.is_informative is True


# ── Beide gleichzeitig: die Anteile duerfen sich nicht vermischen ─────────────

def test_anteile_addieren_sich_zur_gesamtverschiebung(tmp_path):
    """
    Der Normalfall im Betrieb: der Spot ist gelaufen UND es wurde OI bewegt.

    Die Zerlegung ist ein Pfad: erst die alte Kette auf den neuen Spot
    (mechanisch), dann die neue Kette bei diesem Spot (informativ). Beide
    Anteile muessen zusammen exakt die beobachtete Gesamtverschiebung ergeben,
    sonst geht auf dem Weg Bewegung verloren oder wird doppelt gezaehlt.

    Nicht getestet wird, dass der informative Anteil zahlenmaessig unabhaengig
    vom Spot ist — das ist er nicht und kann er nicht sein: Gamma selbst haengt
    am Spot, ein OI-Aufbau wiegt bei 100.800 anders als bei 100.000. Invariant
    ist die Zerlegung, nicht der Betrag.
    """
    from agents.options_agent import calculate_aggregates, parse_chain, resolve_expiry_groups

    now = datetime.now(timezone.utc)
    neue_oi = dict(BASE_OI)
    neue_oi[(101000, "C")] = 1400.0

    agent = _agent(tmp_path)
    agent._prev_chain = _chain(BASE_OI)
    agent._raw_chain = _chain(neue_oi)
    agent._current_spot = 100800.0

    shift = next(s for s in agent.compute_chain_shift(now) if s.label == "0dte")
    assert shift.spot_move == pytest.approx(800.0)
    assert shift.zero_gamma_mechanical is not None
    assert shift.zero_gamma_informative is not None

    # Ausgangs- und Endzustand direkt rechnen und mit der Summe vergleichen
    def zero_gamma_of(chain, spot):
        parsed, _ = parse_chain(chain, spot)
        groups = resolve_expiry_groups(parsed, now)
        eg, _ = calculate_aggregates(groups["0dte"], "0dte", parsed[groups["0dte"]], spot, now)
        return eg.zero_gamma

    vorher = zero_gamma_of(agent._prev_chain, 100000.0)
    nachher = zero_gamma_of(agent._raw_chain, 100800.0)
    assert vorher is not None and nachher is not None

    summe = shift.zero_gamma_mechanical + shift.zero_gamma_informative
    assert summe == pytest.approx(nachher - vorher, rel=1e-9)

    # Und beide Anteile tragen tatsaechlich etwas bei
    assert shift.zero_gamma_mechanical != 0.0
    assert shift.zero_gamma_informative != 0.0
    assert shift.is_informative is True


# ── Randfaelle: keine Zerlegung ohne Vergleichsbasis ──────────────────────────

def test_ohne_vorherige_kette_keine_zerlegung(tmp_path):
    """Beim ersten Kettentakt gibt es nichts zu vergleichen."""
    agent = _agent(tmp_path)
    agent._prev_chain = []
    assert agent.compute_chain_shift(datetime.now(timezone.utc)) == ()


def test_ohne_spot_keine_zerlegung(tmp_path):
    """Ohne Spot laesst sich keine der beiden Ketten rechnen."""
    agent = _agent(tmp_path)
    agent._prev_chain = _chain(BASE_OI)
    agent._current_spot = 0.0
    assert agent.compute_chain_shift(datetime.now(timezone.utc)) == ()


def test_zerlegung_landet_im_snapshot(tmp_path):
    """Der Snapshot traegt die Zerlegung, damit die Anzeige sie trennen kann."""
    from agents.options_agent import snapshot_to_dict

    agent = _agent(tmp_path)
    agent._prev_chain = _chain(BASE_OI)
    neue_oi = dict(BASE_OI)
    neue_oi[(101000, "C")] = 1400.0
    agent._raw_chain = _chain(neue_oi)
    agent._last_shift = agent.compute_chain_shift(datetime.now(timezone.utc))

    snap = agent.recompute()
    assert snap is not None
    assert snap.chain_shift

    payload = snapshot_to_dict(snap)
    entry = next(e for e in payload["chain_shift"] if e["label"] == "0dte")
    assert entry["is_informative"] is True
    assert entry["spot_move"] == 0.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
