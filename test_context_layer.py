"""Market Structure Context Layer — comprehensive test suite."""

import sys
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, ".")

from core.session_profile import (
    SessionProfile, ProfileSnapshot,
    session_name_for_hour, is_pre_session,
    _compute_poc, _compute_value_area, _compute_ohlc, _bucket, BUCKET,
)
from core.weekly_profile import WeeklyProfile
from core.profile_shape import classify_shape, smooth_vap, find_peaks
from core.absorption import AbsorptionDetector
from core.divergence import DivergenceDetector
from core.composite_profile import (
    CompositeProfile, average_vap, find_hvn_zones, find_lvn_zones,
    check_value_area_overlap,
)

PASS = 0
FAIL = 0


def test(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# ====================================================================
# Part 1: Session Profile
# ====================================================================

def test_session_boundaries() -> None:
    print("\n--- Part 1: Session Boundaries ---")
    test("Asia 02:00", session_name_for_hour(2) == "Asia")
    test("Pre-London 07:00", session_name_for_hour(7) == "Pre-London")
    test("London 09:00", session_name_for_hour(9) == "London")
    test("Pre-NY 12:00", session_name_for_hour(12) == "Pre-NY")
    test("New York 14:00", session_name_for_hour(14) == "New York")
    test("Pre-Asia 22:00", session_name_for_hour(22) == "Pre-Asia")
    test("is_pre_session Pre-London", is_pre_session("Pre-London") == True)
    test("is_pre_session London", is_pre_session("London") == False)


def test_session_ingestion_and_archive() -> None:
    print("\n--- Part 2: Session Ingestion + Archive ---")
    sp = SessionProfile()

    asia_ts = int(datetime(2026, 6, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    london_ts = int(datetime(2026, 6, 1, 9, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)

    sp.ingest(asia_ts, 50000.0, 1.0, "buy")
    sp.ingest(asia_ts + 1000, 50100.0, 0.5, "sell")

    ctx = sp.current_context()
    test("Asia session name", ctx.get("session") == "Asia")
    test("Asia has POC", ctx.get("session_poc") is not None)
    test("Asia has OHLC open", ctx.get("session_ohlc", {}).get("open") == 50000.0)

    sp.ingest(london_ts, 50200.0, 2.0, "buy")
    sp.ingest(london_ts + 1000, 50300.0, 1.5, "sell")

    ctx = sp.current_context()
    test("London session name", ctx.get("session") == "London")

    archived = sp.get_archived_profiles()
    test("Asia archived", len(archived) >= 1)
    if archived:
        test("Archived label Asia", archived[0].label == "Asia")
        test("Archived has POC", archived[0].poc is not None)
        test("Archived has VA", archived[0].value_area_high is not None)


def test_session_initial_balance() -> None:
    print("\n--- Part 3: Initial Balance ---")
    sp = SessionProfile(initial_balance_minutes=5)

    start_ts = int(datetime(2026, 6, 1, 8, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    sp.ingest(start_ts, 50000.0, 1.0, "buy")
    sp.ingest(start_ts + 60_000, 50100.0, 0.5, "sell")
    sp.ingest(start_ts + 300_000, 50200.0, 2.0, "buy")
    sp.ingest(start_ts + 360_000, 50300.0, 1.0, "sell")

    ctx = sp.current_context()
    test("IB high", ctx.get("initial_balance_high") == 50200.0)
    test("IB low", ctx.get("initial_balance_low") == 50000.0)

    va_h, va_l = _compute_value_area({50000: 10, 50025: 20, 50050: 5, 50075: 3})
    test("VA computed", va_h is not None and va_l is not None)
    if va_h is not None and va_l is not None:
        test("VA high >= low", va_h >= va_l)


def test_poc_drift() -> None:
    print("\n--- Part 4: POC Drift ---")
    sp = SessionProfile(poc_drift_interval_s=0.04)
    start_ts = int(datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    for i in range(5):
        sp.ingest(start_ts + i * 50, 50000.0 + i, 1.0, "buy")
    ctx = sp.current_context()
    test("POC drift buckets", ctx.get("poc_drift_buckets") is not None)


# ====================================================================
# Part 2: Weekly Profile
# ====================================================================

def test_weekly_boundaries() -> None:
    print("\n--- Part 5: Weekly Boundaries ---")
    from core.weekly_profile import _week_start_ms

    ts = int(datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
    ws_ms, ws_label = _week_start_ms(ts)
    ws_dt = datetime.fromtimestamp(ws_ms / 1000, tz=timezone.utc)
    test("Week starts Sunday", ws_dt.weekday() == 6)
    test("Week starts 22:00", ws_dt.hour == 22)
    test("Week label format", ws_label.startswith("Week-"))

    ts2 = int(datetime(2026, 6, 3, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
    ws2_ms, ws2_label = _week_start_ms(ts2)
    test("Same week", ws_label == ws2_label)

    ts3 = int(datetime(2026, 6, 7, 22, 0, tzinfo=timezone.utc).timestamp() * 1000)
    ws3_ms, ws3_label = _week_start_ms(ts3)
    test("Next week different", ws_label != ws3_label)


def test_weekly_ingest() -> None:
    print("\n--- Part 6: Weekly Ingestion ---")
    wp = WeeklyProfile()
    ts1 = int(datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
    ts2 = int(datetime(2026, 6, 3, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)

    wp.ingest(ts1, 50000.0, 1.0, "buy")
    wp.ingest(ts2, 50100.0, 2.0, "sell")

    ctx = wp.current_context()
    test("Week label present", ctx.get("week", "").startswith("Week-"))
    test("Week has POC", ctx.get("week_poc") is not None)
    test("Week has OHLC", ctx.get("week_ohlc", {}).get("open") is not None)


# ====================================================================
# Part 3: Profile Shape
# ====================================================================

def test_profile_shape() -> None:
    print("\n--- Part 7: Profile Shape ---")

    vap_b = {50000: 20, 50025: 10, 50050: 5, 50075: 2, 50100: 1}
    test("b-Shape", classify_shape(vap_b)["shape"] == "b")

    vap_p = {50000: 1, 50025: 2, 50050: 5, 50075: 10, 50100: 20}
    test("P-Shape", classify_shape(vap_p)["shape"] == "P")

    vap_d = {50000: 5, 50025: 15, 50050: 20, 50075: 15, 50100: 5}
    test("D-Shape", classify_shape(vap_d)["shape"] == "D")

    vap_b2 = {50000: 20, 50025: 5, 50050: 3, 50075: 5, 50100: 18}
    test("B-Shape", classify_shape(vap_b2)["shape"] == "B")

    test("Empty VAP unknown", classify_shape({})["shape"] == "unknown")
    test("None VAP unknown", classify_shape(None)["shape"] == "unknown")


# ====================================================================
# Part 4: Absorption Detection
# ====================================================================

def test_absorption() -> None:
    print("\n--- Part 8: Absorption Detection ---")
    detector = AbsorptionDetector(window_seconds=30, max_price_move_bps=5.0, strength_threshold=1.0)
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)

    for i in range(5):
        e = detector.ingest(
            {"price": 50000.0 + i * 0.1, "size": 0.1, "side": "sell", "timestamp": ts + i * 100},
            cvd_snapshot={"rolling_delta": -0.5}, current_price=50000.0,
        )
    test("Small vol: no absorption", e is None)

    d2 = AbsorptionDetector(window_seconds=30, max_price_move_bps=5.0, strength_threshold=1.0)
    event = None
    for i in range(30):
        event = d2.ingest(
            {"price": 50000.0 + (i % 3) * 0.1, "size": 2.0, "side": "sell", "timestamp": ts + i * 100},
            cvd_snapshot={"rolling_delta": -15.0}, current_price=50000.0,
        )
        if event:
            break
    test("Absorption event", event is not None)
    if event:
        test("Bullish (sell absorbed)", event["absorption_type"] == "bullish")


# ====================================================================
# Part 5: Divergence
# ====================================================================

def test_divergence() -> None:
    print("\n--- Part 9: CVD Divergence ---")
    d = DivergenceDetector(lookback=1)

    data = [
        (1000,   50000.0,  10.0),
        (61000,  50100.0,  9.0),
        (121000, 50200.0,  8.0),
        (181000, 50100.0,  7.0),
        (241000, 50150.0,  7.5),
        (301000, 50400.0,  6.0),
        (361000, 50300.0,  5.5),
    ]
    for ts, pr, cvd in data:
        d.ingest(ts, pr, cvd)

    div = d.current_divergence
    test("Bearish divergence detected", div is not None)
    if div is not None:
        test("Type contains bearish", "bearish" in div["divergence_type"])


# ====================================================================
# Part 6: Composite Profile
# ====================================================================

def test_composite_profile() -> None:
    print("\n--- Part 10: Composite Profile ---")

    snap1 = ProfileSnapshot(
        label="NY-1", timestamp=1000,
        ohlc={"open": 50000, "high": 50100, "low": 49900, "close": 50050},
        poc=50000, value_area_high=50125, value_area_low=49900,
        total_volume=100, bucket_count=7,
        vap={49900: 10, 49925: 20, 50000: 40, 50025: 1, 50050: 0.5,
             50100: 30, 50125: 35, 50200: 10},
        poc_drift=[50000, 50000, 50025],
    )
    snap2 = ProfileSnapshot(
        label="NY-2", timestamp=2000,
        ohlc={"open": 50050, "high": 50200, "low": 50100, "close": 50150},
        poc=50150, value_area_high=50300, value_area_low=50200,
        total_volume=80, bucket_count=4,
        vap={50100: 10, 50125: 30, 50150: 30, 50200: 10},
        poc_drift=[50100, 50150, 50150],
    )

    comp = CompositeProfile()
    comp.add_profiles([snap1, snap2])
    ctx = comp.current_context()

    test("Composite has POC", ctx.get("composite_poc") is not None)
    test("Composite count", ctx.get("profile_count") == 2)
    test("Balance flag set", ctx.get("balance_flag", "") in ("balance", "imbalance_up", "imbalance_down"))

    composite_vap = average_vap([snap1, snap2])
    hvn = find_hvn_zones(composite_vap, threshold_pct=0.05)
    test("HVN zones found", len(hvn) > 0)

    lvn = find_lvn_zones(composite_vap, max_vol_pct=0.05)
    test("LVN zones found", len(lvn) > 0)
    if lvn:
        test("LVN zone price_low < price_high", lvn[0]["price_low"] < lvn[0]["price_high"])

    overlap = check_value_area_overlap(snap1, snap2)
    test("VA non-overlap detected", overlap["overlap"] == False)


# ====================================================================
# Part 7: Pre-session Anomaly
# ====================================================================

def test_pre_session_anomaly() -> None:
    print("\n--- Part 11: Pre-Session Anomaly ---")

    sp = SessionProfile()
    sp._ps_baseline = {"Pre-Asia": 200.0, "Pre-London": 150.0, "Pre-NY": 100.0}
    sp.anomaly_factor_threshold = 1.5

    pre_ns = int(datetime(2026, 6, 1, 6, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    sp.ingest(pre_ns, 50000.0, 0.1, "buy")
    anomaly = sp.get_pre_session_anomaly("Pre-London")
    test("Low volume: no anomaly", anomaly is None)

    sp2 = SessionProfile()
    sp2._ps_baseline = {"Pre-Asia": 200.0, "Pre-London": 150.0, "Pre-NY": 100.0}
    sp2.anomaly_factor_threshold = 1.5
    for i in range(100):
        sp2.ingest(pre_ns + i * 100, 50000.0, 10.0, "buy")

    anomaly2 = sp2.get_pre_session_anomaly("Pre-London")
    test("High volume: anomaly detected", anomaly2 is not None)
    if anomaly2 is not None:
        test("Anomaly factor > threshold", anomaly2["anomaly_factor"] > 1.5)

    sp3 = SessionProfile()
    bl = sp3.build_pre_session_baseline_from_parquet(Path("data/historical"))
    test("Build baseline returns 3 phases", len(bl) == 3)
    for phase in ["Pre-Asia", "Pre-London", "Pre-NY"]:
        test(f"Baseline has {phase}", phase in bl)
        test(f"  {phase} > 0", bl[phase] > 0)


# ====================================================================
# Main
# ====================================================================

def main() -> int:
    print("=" * 60)
    print("  Market Structure Context Layer — Test Suite")
    print("=" * 60)

    test_session_boundaries()
    test_session_ingestion_and_archive()
    test_session_initial_balance()
    test_poc_drift()
    test_weekly_boundaries()
    test_weekly_ingest()
    test_profile_shape()
    test_absorption()
    test_divergence()
    test_composite_profile()
    test_pre_session_anomaly()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"  RESULT: {PASS} passed, {FAIL} failed (out of {total})")
    print(f"{'=' * 60}")
    return 1 if FAIL > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
