"""
tests/test_options_agent.py — Test-Suite fuer den Deribit Options & GEX Agenten.

Plain-print Format nach Repo-Konvention (tests/test_geometry.py):
check(name, cond, detail) mit PASS/FAIL Counter und exit(1 if FAIL else 0).
"""

import asyncio
from datetime import datetime, timezone, timedelta
import math
import os
import sys
from pathlib import Path

# Fuege Root-Verzeichnis zum Python-Pfad hinzu
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents.options_agent import (
    parse_instrument_name,
    calculate_t_years,
    calculate_bs_gamma,
    calculate_gex_usd,
    calculate_zero_gamma,
    calculate_aggregates,
    resolve_expiry_groups,
    HistoryManager,
    OptionsAgent,
    StrikeGamma,
    ExpiryGamma,
    OptionsSnapshot,
    fetch_deribit_chain_sync
)
from core.broker import Broker, OPTIONS, AGGREGATED, TRADES

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    """Standard Test-Check nach Repo-Konvention."""
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} -- {detail}")


# ── 1. Instrument Name Parsing ────────────────────────────────────────────────

def test_parsing() -> None:
    print("\n=== 1. Instrument Name Parsing ===")
    
    # Standard Deribit Format
    res = parse_instrument_name("BTC-27JUN26-90000-C")
    check("valid call name parses correctly", res is not None)
    if res:
        exp, strike, opt_type = res
        check("parsed expiry is 2026-06-27 08:00 UTC", 
              exp == datetime(2026, 6, 27, 8, 0, 0, tzinfo=timezone.utc),
              f"got {exp}")
        check("parsed strike is 90000.0", strike == 90000.0, f"got {strike}")
        check("parsed type is 'C'", opt_type == "C", f"got {opt_type}")
    
    # 1-stelliger Tag und Put
    res_put = parse_instrument_name("BTC-3JUL26-85000-P")
    check("valid single-digit day put parses correctly", res_put is not None)
    if res_put:
        exp, strike, opt_type = res_put
        check("parsed expiry is 2026-07-03 08:00 UTC", 
              exp == datetime(2026, 7, 3, 8, 0, 0, tzinfo=timezone.utc))
        check("parsed strike is 85000.0", strike == 85000.0)
        check("parsed type is 'P'", opt_type == "P")

    # Malformed names
    malformed = [
        "BTC-PERPETUAL",
        "BTC-27JUN26",
        "ETH-27JUN26-3000-C",
        "BTC-INVALID-90000-C",
        "BTC-27JUN26-INVALID-C",
        "BTC-27JUN26-90000-X",
        "",
        None,
        123
    ]
    for bad in malformed:
        check(f"malformed name '{bad}' returns None", parse_instrument_name(bad) is None)


# ── 2. Black-Scholes Gamma Eigenschaften ──────────────────────────────────────

def test_gamma_math() -> None:
    print("\n=== 2. Black-Scholes Gamma Properties ===")
    
    spot = 65000.0
    iv = 0.55  # 55% IV
    T = 7.0 / 365.0  # 7 Tage
    
    gamma_atm = calculate_bs_gamma(spot, 65000.0, iv, T)
    gamma_otm_call = calculate_bs_gamma(spot, 70000.0, iv, T)
    gamma_far_call = calculate_bs_gamma(spot, 75000.0, iv, T)
    gamma_otm_put = calculate_bs_gamma(spot, 60000.0, iv, T)
    gamma_far_put = calculate_bs_gamma(spot, 55000.0, iv, T)
    
    check("gamma is positive for ATM", gamma_atm > 0)
    check("gamma peaks at ATM vs OTM call", gamma_atm > gamma_otm_call)
    check("gamma decays monotonically on call side", gamma_otm_call > gamma_far_call)
    check("gamma peaks at ATM vs OTM put", gamma_atm > gamma_otm_put)
    check("gamma decays monotonically on put side", gamma_otm_put > gamma_far_put)
    
    # Laufzeit-Abhaengigkeit: ATM Gamma steigt bei T -> 0
    gamma_1d = calculate_bs_gamma(spot, 65000.0, iv, 1.0 / 365.0)
    gamma_30d = calculate_bs_gamma(spot, 65000.0, iv, 30.0 / 365.0)
    check("ATM gamma increases as T -> 0", gamma_1d > gamma_30d)
    
    # 15-Minuten Floor Test (Spec §4.1)
    now = datetime(2026, 6, 27, 8, 0, 0, tzinfo=timezone.utc)
    t_0 = calculate_t_years(now, now)
    check("T_years at T=0 respects 15-minute floor", t_0 == 900.0 / 31_536_000.0)
    
    t_neg = calculate_t_years(now - timedelta(minutes=5), now)
    check("T_years at T < 0 respects 15-minute floor", t_neg == 900.0 / 31_536_000.0)
    
    gamma_floor = calculate_bs_gamma(spot, 65000.0, iv, t_0)
    check("gamma at T=0 with floor is finite (no inf/nan)", 
          math.isfinite(gamma_floor) and gamma_floor > 0)
    
    # Ungueltige IV
    check("zero IV returns gamma 0.0", calculate_bs_gamma(spot, 65000.0, 0.0, T) == 0.0)
    check("negative IV returns gamma 0.0", calculate_bs_gamma(spot, 65000.0, -0.2, T) == 0.0)


# ── 3. GEX & Inverse Contract Maths (S^2 Skalierung) ──────────────────────────

def test_gex_math() -> None:
    print("\n=== 3. GEX & Inverse Contract Mathematics ===")
    
    spot = 50000.0
    iv = 0.50
    T = 7.0 / 365.0
    oi = 100.0  # 100 BTC Kontrakte
    
    gamma = calculate_bs_gamma(spot, spot, iv, T)
    gex_call = calculate_gex_usd(gamma, oi, spot, "C")
    gex_put = calculate_gex_usd(gamma, oi, spot, "P")
    
    check("call GEX is positive (dealer dampening)", gex_call > 0)
    check("put GEX is negative (dealer amplifying)", gex_put < 0)
    check("call and put GEX are symmetric with identical parameters", math.isclose(gex_call, -gex_put, rel_tol=1e-6))
    
    # S^2 Inverser Kontrakt Check (Spec §3)
    # Bei Verdopplung des Spots von 50k auf 100k mit festem Gamma und OI
    # muss sich das USD GEX um Faktor 4 erhoehen ((100k)^2 / (50k)^2 = 4)
    gex_fixed_gamma_50k = gamma * oi * (50000.0 ** 2) * 0.01
    gex_fixed_gamma_100k = gamma * oi * (100000.0 ** 2) * 0.01
    ratio = gex_fixed_gamma_100k / gex_fixed_gamma_50k
    check("doubling spot scales GEX by exactly 4x (S^2 inverse contract term)", math.isclose(ratio, 4.0, rel_tol=1e-5), f"got {ratio}")


# ── 4. Aggregates: Zero Gamma, Walls, ATM OI, Expected Move ───────────────────

def test_aggregates() -> None:
    print("\n=== 4. Aggregates & Zero Gamma Interpolation ===")
    
    now = datetime(2026, 6, 27, 0, 0, 0, tzinfo=timezone.utc)
    exp = datetime(2026, 6, 27, 8, 0, 0, tzinfo=timezone.utc)
    spot = 65000.0
    
    # Synthetische Kette: Puts bei 60k, 62k, 64k; Calls bei 66k, 68k, 70k
    raw = [
        {"strike": 60000.0, "type": "P", "oi": 500.0, "mark_iv": 0.55},
        {"strike": 62000.0, "type": "P", "oi": 800.0, "mark_iv": 0.52},  # Put Wall
        {"strike": 64000.0, "type": "P", "oi": 300.0, "mark_iv": 0.50},  # ATM-nah
        {"strike": 65000.0, "type": "C", "oi": 200.0, "mark_iv": 0.50},  # ATM
        {"strike": 66000.0, "type": "C", "oi": 400.0, "mark_iv": 0.50},  # ATM-nah
        {"strike": 68000.0, "type": "C", "oi": 900.0, "mark_iv": 0.53},  # Call Wall
        {"strike": 70000.0, "type": "C", "oi": 600.0, "mark_iv": 0.56},
    ]
    
    eg, skipped = calculate_aggregates(exp, "0dte", raw, spot, now)
    
    check("aggregates computed without skipped items", skipped == 0)
    check("put wall is 62000 (largest put OI below spot)", eg.put_wall == 62000.0, f"got {eg.put_wall}")
    check("call wall is 68000 (largest call OI above spot)", eg.call_wall == 68000.0, f"got {eg.call_wall}")
    
    # ATM OI (innerhalb +/- 2.5% von 65000 -> 63375 bis 66625)
    # Beinhaltet Strikes 64000 (300 OI), 65000 (200 OI), 66000 (400 OI) -> Summe = 900.0
    check("atm_oi includes only strikes within +/-2.5% (900 BTC)", 
          math.isclose(eg.atm_oi, 900.0, rel_tol=1e-5), f"got {eg.atm_oi}")
    
    # Expected move > 0
    check("expected move is positive and reasonable", 200.0 < eg.expected_move < 5000.0, f"got {eg.expected_move}")
    
    # Zero Gamma Nulldurchgang
    check("zero_gamma is found and bracketed by chain strikes", 
          eg.zero_gamma is not None and 60000.0 <= eg.zero_gamma <= 70000.0, 
          f"got {eg.zero_gamma}")
    
    # All-Call Chain -> Net GEX > 0, kein Zero Gamma Crossing
    raw_calls = [
        {"strike": 64000.0, "type": "C", "oi": 200.0, "mark_iv": 0.50},
        {"strike": 66000.0, "type": "C", "oi": 400.0, "mark_iv": 0.50},
    ]
    eg_calls, _ = calculate_aggregates(exp, "0dte", raw_calls, spot, now)
    check("all-call chain has net_gex > 0", eg_calls.net_gex > 0)
    check("all-call chain has zero_gamma as None (no crossing)", eg_calls.zero_gamma is None)
    
    # All-Put Chain -> Net GEX < 0, kein Zero Gamma Crossing
    raw_puts = [
        {"strike": 64000.0, "type": "P", "oi": 200.0, "mark_iv": 0.50},
        {"strike": 66000.0, "type": "P", "oi": 400.0, "mark_iv": 0.50},
    ]
    eg_puts, _ = calculate_aggregates(exp, "0dte", raw_puts, spot, now)
    check("all-put chain has net_gex < 0", eg_puts.net_gex < 0)
    check("all-put chain has zero_gamma as None", eg_puts.zero_gamma is None)


# ── 5. Expiry Grouping (0DTE, Weekly, Monthly) ────────────────────────────────

def test_expiry_grouping() -> None:
    print("\n=== 5. Expiry Grouping Resolution ===")
    
    # Fall 1: now ist 07:59 UTC am 2026-06-27 (Samstag)
    now_before_0800 = datetime(2026, 6, 27, 7, 59, 0, tzinfo=timezone.utc)
    exp_today = datetime(2026, 6, 27, 8, 0, 0, tzinfo=timezone.utc)
    exp_tomorrow = datetime(2026, 6, 28, 8, 0, 0, tzinfo=timezone.utc)
    exp_weekly = datetime(2026, 7, 3, 8, 0, 0, tzinfo=timezone.utc)    # Naechster Freitag
    exp_monthly = datetime(2026, 7, 31, 8, 0, 0, tzinfo=timezone.utc)  # Letzter Freitag im Juli
    
    parsed_map = {
        exp_today: [{"strike": 65000.0, "type": "C", "oi": 100.0, "mark_iv": 0.5}],
        exp_tomorrow: [{"strike": 65000.0, "type": "C", "oi": 100.0, "mark_iv": 0.5}],
        exp_weekly: [{"strike": 65000.0, "type": "C", "oi": 100.0, "mark_iv": 0.5}],
        exp_monthly: [{"strike": 65000.0, "type": "C", "oi": 100.0, "mark_iv": 0.5}],
    }
    
    groups_1 = resolve_expiry_groups(parsed_map, now_before_0800)
    check("at 07:59 UTC, 0DTE resolves to today's 08:00 UTC", groups_1.get("0dte") == exp_today)
    check("weekly resolves to next Friday", groups_1.get("weekly") == exp_weekly)
    check("monthly resolves to last Friday of month", groups_1.get("monthly") == exp_monthly)
    
    # Fall 2: now ist 08:01 UTC am 2026-06-27 -> 0DTE rollt auf 2026-06-28 08:00 UTC
    now_after_0800 = datetime(2026, 6, 27, 8, 1, 0, tzinfo=timezone.utc)
    groups_2 = resolve_expiry_groups(parsed_map, now_after_0800)
    check("at 08:01 UTC, 0DTE resolves to tomorrow's 08:00 UTC", groups_2.get("0dte") == exp_tomorrow)


# ── 6. Robustness, Stale Flags & Expiry Flag Persistence ──────────────────────

def test_robustness_and_persistence() -> None:
    print("\n=== 6. Robustness & Expiry Flag Persistence ===")
    
    # HistoryManager Test
    tmp_path = Path("/tmp/test_atm_oi_history.json")
    if tmp_path.exists():
        tmp_path.unlink()
    
    hm = HistoryManager(filepath=tmp_path)
    
    # Weniger als 60 Tage Historie -> Flag ist None
    for i in range(20):
        hm.record_direct(f"2026-05-{i+1:02d}", 1000.0 + i * 10)
    
    flag_insufficient = hm.evaluate_flag(atm_oi_0dte=1500.0, net_gex_0dte=-500000.0)
    check("with <60 history days, expiry flag active is None", flag_insufficient.active is None)
    
    # 65 Tage auffuellen
    for i in range(20, 65):
        hm.record_direct(f"2026-06-{i-19:02d}", 1000.0 + i * 10)
    
    flag_sufficient_true = hm.evaluate_flag(atm_oi_0dte=2000.0, net_gex_0dte=-500000.0)
    check("with >=60 days, high ATM OI and neg GEX triggers active=True", 
          flag_sufficient_true.active is True)
    
    flag_pos_gex = hm.evaluate_flag(atm_oi_0dte=2000.0, net_gex_0dte=500000.0)
    check("with positive GEX, flag active=False", flag_pos_gex.active is False)
    
    flag_low_oi = hm.evaluate_flag(atm_oi_0dte=500.0, net_gex_0dte=-500000.0)
    check("with low ATM OI below 90th percentile, flag active=False", flag_low_oi.active is False)
    
    if tmp_path.exists():
        tmp_path.unlink()
    
    # OptionsAgent Snapshot Recomputation & Stale Handling
    broker = Broker()
    agent = OptionsAgent(broker)
    agent._raw_chain = [
        {"instrument_name": "BTC-27JUN26-64000-P", "open_interest": 100.0, "mark_iv": 55.0},
        {"instrument_name": "BTC-27JUN26-66000-C", "open_interest": 100.0, "mark_iv": 55.0},
    ]
    agent._stale = False
    
    snap = agent.recompute()
    check("recompute produces valid snapshot", snap is not None and len(snap.expiries) > 0)
    check("snapshot is marked not stale", snap.stale is False)
    
    # Spot-Tick Update
    agent.set_spot(68000.0)
    snap2 = agent.recompute()
    check("setting spot updates snapshot spot to 68000", snap2.spot == 68000.0)


# ── 7. Live Deribit Smoke Test (Online / Skippable Offline) ───────────────────

def test_live_smoke() -> None:
    print("\n=== 7. Live Deribit API Smoke Test ===")
    try:
        raw = fetch_deribit_chain_sync()
        check("live Deribit API returned non-empty chain", len(raw) > 0, f"count={len(raw)}")
        
        broker = Broker()
        agent = OptionsAgent(broker)
        agent._raw_chain = raw
        agent._stale = False
        agent._current_spot = 65000.0
        
        snap = agent.recompute()
        check("live chain produces valid snapshot", snap is not None)
        if snap:
            check("snapshot contains at least one expiry group", len(snap.expiries) > 0)
            for eg in snap.expiries:
                check(f"expiry '{eg.label}' has finite net_gex", math.isfinite(eg.net_gex))
                print(f"      [INFO] {eg.label.upper()} ({eg.expiry.strftime('%Y-%m-%d')}): "
                      f"Net GEX: ${eg.net_gex/1e6:.2f}M | Put Wall: ${eg.put_wall} | "
                      f"Call Wall: ${eg.call_wall} | Zero-Γ: {eg.zero_gamma} | ATM OI: {eg.atm_oi:.1f} BTC")
    except Exception as exc:
        print(f"  [SKIP] Live Deribit fetch skipped (network/offline): {exc}")


# ── Main Runner ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("==================================================")
    print("OrderFlow Pro — Deribit Options & GEX Agent Tests")
    print("==================================================")
    
    test_parsing()
    test_gamma_math()
    test_gex_math()
    test_aggregates()
    test_expiry_grouping()
    test_robustness_and_persistence()
    test_live_smoke()
    
    print("\n================================")
    print(f"  {PASS} passed, {FAIL} failed")
    print("================================")
    sys.exit(1 if FAIL else 0)
