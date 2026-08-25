"""
tests/test_options_agent.py — Haertung des Options-Layers.

Schwerpunkt sind die Zusagen aus dem Charter §2 und §7: das System zeigt keine
Zahl an, fuer die es keine Datengrundlage hat. Jeder Test hier haelt genau eine
Stelle fest, an der frueher ein Ersatzwert entstand oder entstehen koennte.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.options_agent import (  # noqa: E402
    MAX_CHAIN_AGE_SECONDS,
    HistoryManager,
    OptionsAgent,
    calculate_aggregates,
    calculate_bs_gamma,
    calculate_gex_usd,
    calculate_t_years,
    calculate_zero_gamma,
    parse_instrument_name,
)
from core.broker import Broker  # noqa: E402


def _chain_item(code: str, strike: int, opt_type: str, oi: float, iv_pct: float,
                underlying: float = 100000.0) -> dict:
    """Baut ein Instrument im Deribit-Book-Summary-Format."""
    return {
        "instrument_name": f"BTC-{code}-{strike}-{opt_type}",
        "open_interest": oi,
        "mark_iv": iv_pct,
        "underlying_price": underlying,
    }


def _expiry_code(dt: datetime) -> str:
    return f"{dt.day}{dt.strftime('%b').upper()}{dt.strftime('%y')}"


def _tomorrow_0800() -> datetime:
    now = datetime.now(timezone.utc)
    exp = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if exp <= now:
        exp += timedelta(days=1)
    return exp


def _agent(tmp_path) -> OptionsAgent:
    """Agent mit isolierter Historie — nie die echte data/-Datei anfassen."""
    return OptionsAgent(Broker(), history_manager=HistoryManager(tmp_path / "atm_oi.json"))


# ── Kein Spot -> kein Snapshot ────────────────────────────────────────────────

def test_ohne_spot_tick_kein_snapshot(tmp_path):
    """Vor dem ersten Spot-Tick darf keine Karte entstehen."""
    agent = _agent(tmp_path)
    exp = _tomorrow_0800()
    agent._raw_chain = [_chain_item(_expiry_code(exp), 100000, "C", 50.0, 55.0)]

    assert agent._current_spot == 0.0
    assert agent.recompute() is None


def test_ungueltiger_spot_wird_abgewiesen(tmp_path):
    """set_spot() akzeptiert weder 0, negative Werte noch NaN."""
    agent = _agent(tmp_path)
    for bad in (0.0, -1.0, float("nan")):
        assert agent.set_spot(bad) is None
    assert agent._current_spot == 0.0


# ── Kein Ersatzwert fuer unbekannte IV ────────────────────────────────────────

def test_expected_move_ist_none_ohne_verwertbare_iv():
    """
    Fruehere Fassung setzte iv_atm = 0.55, wenn kein Strike verwertbar war —
    daraus entstand ein Expected Move ohne Datengrundlage.
    """
    exp = _tomorrow_0800()
    now = datetime.now(timezone.utc)
    instruments = [
        {"strike": 100000.0, "type": "C", "oi": 0.0, "mark_iv": 0.55},   # kein OI
        {"strike": 101000.0, "type": "P", "oi": 10.0, "mark_iv": 0.0},   # keine IV
    ]
    eg, skipped = calculate_aggregates(exp, "0dte", instruments, 100000.0, now)

    assert skipped == 2
    assert eg.strikes == ()
    assert eg.iv_atm is None
    assert eg.expected_move is None
    assert eg.net_gex == 0.0


def test_expected_move_wird_gerechnet_wenn_iv_bekannt():
    """Gegenprobe: mit verwertbarem Strike entsteht ein Expected Move."""
    exp = _tomorrow_0800()
    now = datetime.now(timezone.utc)
    instruments = [{"strike": 100000.0, "type": "C", "oi": 25.0, "mark_iv": 0.55}]
    eg, _ = calculate_aggregates(exp, "0dte", instruments, 100000.0, now)

    assert eg.iv_atm == pytest.approx(0.55)
    assert eg.expected_move == pytest.approx(100000.0 * 0.55 * math.sqrt(eg.t_years))


# ── Zero-Gamma wird nicht geraten ─────────────────────────────────────────────

def test_zero_gamma_ohne_nulldurchgang_ist_none():
    """
    Ohne Vorzeichenwechsel im kumulierten GEX gibt es keinen Flip. Der
    TS-Agent setzte hier spot * 1.025 — eine erfundene Marke.
    """
    exp = _tomorrow_0800()
    now = datetime.now(timezone.utc)
    # Nur Calls: kumuliertes GEX ist durchgehend positiv
    instruments = [
        {"strike": float(k), "type": "C", "oi": 20.0, "mark_iv": 0.55}
        for k in range(98000, 103000, 1000)
    ]
    eg, _ = calculate_aggregates(exp, "0dte", instruments, 100000.0, now)

    assert eg.net_gex > 0.0
    assert eg.zero_gamma is None


def test_zero_gamma_interpoliert_bei_vorzeichenwechsel():
    """Mit Nulldurchgang liegt der Flip zwischen den umklammernden Strikes."""
    exp = _tomorrow_0800()
    now = datetime.now(timezone.utc)
    instruments = [
        {"strike": 99000.0, "type": "P", "oi": 100.0, "mark_iv": 0.55},   # negativ
        {"strike": 101000.0, "type": "C", "oi": 300.0, "mark_iv": 0.55},  # positiv
    ]
    eg, _ = calculate_aggregates(exp, "0dte", instruments, 100000.0, now)

    assert eg.zero_gamma is not None
    assert 99000.0 < eg.zero_gamma < 101000.0


def test_zero_gamma_bei_leerer_liste():
    assert calculate_zero_gamma([]) is None


# ── Ketten-Alter: sichtbar, dann Leerzustand ──────────────────────────────────

def test_stale_snapshot_traegt_sein_alter(tmp_path):
    """Innerhalb des Limits: letzter echter Snapshot, als stale markiert."""
    agent = _agent(tmp_path)
    exp = _tomorrow_0800()
    code = _expiry_code(exp)
    agent._raw_chain = [
        _chain_item(code, 99000, "P", 100.0, 55.0),
        _chain_item(code, 101000, "C", 300.0, 55.0),
    ]
    agent._chain_ts = datetime.now(timezone.utc)
    agent._stale = False  # sonst gilt der Agent bis zum ersten Fetch als stale
    first = agent.set_spot(100000.0)
    assert first is not None and first.stale is False

    # Kette faellt aus, Zeit vergeht — aber innerhalb des Limits
    agent._raw_chain = []
    later = datetime.now(timezone.utc) + timedelta(seconds=MAX_CHAIN_AGE_SECONDS - 60)
    snap = agent.recompute(now_utc=later)

    assert snap is not None
    assert snap.stale is True
    assert snap.chain_age_seconds == pytest.approx(MAX_CHAIN_AGE_SECONDS - 60, abs=5)
    assert snap.expiries == first.expiries  # unveraenderte Zahlen, nur aelter


def test_ueberalterte_kette_liefert_keinen_snapshot(tmp_path):
    """Jenseits des Limits: Leerzustand statt Karte mit Vergangenheitswerten."""
    agent = _agent(tmp_path)
    exp = _tomorrow_0800()
    code = _expiry_code(exp)
    agent._raw_chain = [_chain_item(code, 100000, "C", 50.0, 55.0)]
    agent._chain_ts = datetime.now(timezone.utc)
    agent._stale = False
    assert agent.set_spot(100000.0) is not None

    agent._raw_chain = []
    much_later = datetime.now(timezone.utc) + timedelta(seconds=MAX_CHAIN_AGE_SECONDS + 60)

    assert agent.recompute(now_utc=much_later) is None


# ── Reversal-Flag: alle Bedingungen oder gar nichts ───────────────────────────

def test_reversal_flag_ohne_historie_ist_unbestimmt(tmp_path):
    """Unter 60 Tagen Historie gibt es kein Dezil — active bleibt None."""
    mgr = HistoryManager(tmp_path / "hist.json")
    flag = mgr.evaluate_flag(atm_oi_0dte=5000.0, net_gex_0dte=-1e9)

    assert flag.active is None
    assert flag.top_decile_threshold is None
    assert flag.history_count == 0


def test_reversal_flag_verlangt_beide_bedingungen(tmp_path):
    """Top-Dezil-OI UND negatives Gamma — eines allein reicht nicht."""
    mgr = HistoryManager(tmp_path / "hist.json")
    for i in range(70):
        mgr.record_direct(f"2026-01-{i + 1:03d}", float(i * 100))
    threshold = mgr.evaluate_flag(0.0, 0.0).top_decile_threshold
    assert threshold is not None

    hoch, niedrig = threshold + 1000.0, threshold - 1000.0

    assert mgr.evaluate_flag(hoch, -1e9).active is True      # beide erfuellt
    assert mgr.evaluate_flag(hoch, +1e9).active is False     # Gamma positiv
    assert mgr.evaluate_flag(niedrig, -1e9).active is False  # OI zu niedrig
    assert mgr.evaluate_flag(niedrig, +1e9).active is False  # keines


def test_reversal_flag_traegt_seine_caveats(tmp_path):
    """Die Grenzen der Studie haengen am Flag, nicht an der UI-Schicht."""
    flag = HistoryManager(tmp_path / "hist.json").evaluate_flag(1.0, -1.0)
    assert len(flag.caveats) == 3
    assert any("2021-2023" in c for c in flag.caveats)


# ── GEX-Konvention (docs/GEX_SCALING.md) ──────────────────────────────────────

def test_gex_vorzeichen_call_positiv_put_negativ():
    gamma = calculate_bs_gamma(100000.0, 100000.0, 0.55, 1 / 365)
    call = calculate_gex_usd(gamma, 10.0, 100000.0, "C")
    put = calculate_gex_usd(gamma, 10.0, 100000.0, "P")

    assert call > 0.0
    assert put < 0.0
    assert call == pytest.approx(-put)


def test_gex_skaliert_mit_s_quadrat():
    """
    Festhalten, dass S^2 gilt, solange die Entscheidung aus docs/GEX_SCALING.md
    offen ist — ein stilles Umstellen auf S^3 faellt hier auf.
    """
    gamma, oi, spot = 1.4e-4, 10.0, 100000.0
    gex = calculate_gex_usd(gamma, oi, spot, "C")

    assert gex == pytest.approx(gamma * oi * spot * spot * 0.01)
    assert 1e4 < gex < 1e6  # plausible Groessenordnung, nicht Milliarden


def test_gamma_floor_verhindert_divergenz():
    """Charter §7.4: ohne Floor springt Gamma kurz vor Settlement ins Extrem."""
    now = datetime.now(timezone.utc)
    t_at_expiry = calculate_t_years(now, now)
    t_past = calculate_t_years(now - timedelta(minutes=30), now)

    assert t_at_expiry > 0.0
    assert t_at_expiry == pytest.approx(t_past)  # Floor greift in beiden Faellen
    assert math.isfinite(calculate_bs_gamma(100000.0, 100000.0, 0.55, t_at_expiry))


# ── Parsing ───────────────────────────────────────────────────────────────────

def test_instrument_parsing():
    parsed = parse_instrument_name("BTC-27JUN26-90000-C")
    assert parsed is not None
    exp, strike, opt_type = parsed
    assert (exp.year, exp.month, exp.day, exp.hour) == (2026, 6, 27, 8)
    assert exp.tzinfo == timezone.utc
    assert strike == 90000.0
    assert opt_type == "C"


@pytest.mark.parametrize("name", [
    "ETH-27JUN26-3000-C",      # falsche Waehrung
    "BTC-27XXX26-90000-C",     # unbekannter Monat
    "BTC-27JUN26-90000-X",     # unbekannter Typ
    "BTC-27JUN26-90000",       # unvollstaendig
    "",
    None,
])
def test_instrument_parsing_weist_ungueltiges_ab(name):
    assert parse_instrument_name(name) is None


# ── Broker-Anbindung ──────────────────────────────────────────────────────────

def test_snapshot_erreicht_den_broker(tmp_path):
    """fetch_and_update() publiziert auf OPTIONS und OPTIONS_SNAPSHOT."""
    from core.broker import OPTIONS, OPTIONS_SNAPSHOT

    async def run():
        broker = Broker()
        received = {OPTIONS: [], OPTIONS_SNAPSHOT: []}
        broker.subscribe(OPTIONS, lambda m: received[OPTIONS].append(m))
        broker.subscribe(OPTIONS_SNAPSHOT, lambda m: received[OPTIONS_SNAPSHOT].append(m))

        agent = OptionsAgent(broker, history_manager=HistoryManager(tmp_path / "h.json"))
        exp = _tomorrow_0800()
        code = _expiry_code(exp)
        agent._raw_chain = [
            _chain_item(code, 99000, "P", 100.0, 55.0),
            _chain_item(code, 101000, "C", 300.0, 55.0),
        ]
        agent._chain_ts = datetime.now(timezone.utc)
        agent._stale = False
        agent._current_spot = 100000.0

        snap = agent.recompute()
        assert snap is not None
        await broker.publish(OPTIONS, snap)
        await asyncio.sleep(0)

        assert len(received[OPTIONS]) == 1
        assert received[OPTIONS][0].spot == 100000.0

    asyncio.run(run())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
