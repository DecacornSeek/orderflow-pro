"""SessionProfile — PR4a test suite.

Covers: ingest_trade, snapshot, reset_if_needed, POC, value area,
regime heuristic, session boundary transitions, deterministic replay.

Run: python tests/test_session_profile.py
"""

import sys
sys.path.insert(0, ".")

from datetime import datetime, timezone

from core.session_profile import SessionProfile, session_name_for_hour, BUCKET

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        detail_str = f"  ({detail})" if detail else ""
        print(f"  [FAIL] {name}{detail_str}")


def ms(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp() * 1000)


# Fixed UTC timestamps. June 2026: June 1 = Monday.
ASIA_TS      = ms(2026, 6, 1,  2)   # 02:00 UTC → Asia
LONDON_TS    = ms(2026, 6, 1,  9)   # 09:00 UTC → London
NY_TS        = ms(2026, 6, 1, 14)   # 14:00 UTC → New York
PRE_ASIA_TS  = ms(2026, 6, 1, 22)   # 22:00 UTC → Pre-Asia


# ---------------------------------------------------------------------------
# 1. Basic ingestion + snapshot fields
# ---------------------------------------------------------------------------

def test_ingest_trade_basic() -> None:
    print("\n--- 1. ingest_trade + snapshot fields ---")
    sp = SessionProfile()
    sp.ingest_trade(65000.0, 1.0, ASIA_TS)
    sp.ingest_trade(65100.0, 2.0, ASIA_TS + 1_000)

    snap = sp.snapshot()
    check("session name = Asia",       snap.get("session") == "Asia")
    check("poc not None",              snap.get("session_poc") is not None)
    check("volume = 3.0",              abs(snap.get("session_volume", -1) - 3.0) < 1e-9)
    check("trade_count = 2",           snap.get("session_trade_count") == 2)
    check("regime field present",      "regime" in snap)
    check("ohlc open = 65000",         snap.get("session_ohlc", {}).get("open") == 65000.0)
    check("ohlc close = 65100",        snap.get("session_ohlc", {}).get("close") == 65100.0)


# ---------------------------------------------------------------------------
# 2. POC correctness
# ---------------------------------------------------------------------------

def test_poc_correctness() -> None:
    print("\n--- 2. POC at max-volume bucket ---")
    sp = SessionProfile()
    for _ in range(10):
        sp.ingest_trade(65000.0, 1.0, ASIA_TS)
    for _ in range(3):
        sp.ingest_trade(65100.0, 1.0, ASIA_TS + 1_000)
    sp.ingest_trade(65200.0, 1.0, ASIA_TS + 2_000)

    expected_poc = (int(65000 / BUCKET)) * BUCKET
    snap = sp.snapshot()
    check("poc = max-vol bucket", snap.get("session_poc") == expected_poc,
          f"got {snap.get('session_poc')!r}, expected {expected_poc}")


# ---------------------------------------------------------------------------
# 3. Value area covers ≥ 70 % of volume
# ---------------------------------------------------------------------------

def test_value_area_coverage() -> None:
    print("\n--- 3. Value area >= 70 % coverage ---")
    sp = SessionProfile(value_area_pct=0.7)
    trades = [
        (64900.0, 1.0), (64925.0, 3.0), (64950.0, 10.0),
        (64975.0, 8.0),  (65000.0, 6.0), (65025.0, 2.0),
        (65050.0, 1.0),
    ]
    for price, size in trades:
        sp.ingest_trade(price, size, ASIA_TS)

    snap = sp.snapshot()
    va_h = snap.get("session_value_area_high")
    va_l = snap.get("session_value_area_low")
    total_vol = sum(s for _, s in trades)

    check("va_high not None", va_h is not None)
    check("va_low not None",  va_l is not None)
    if va_h is not None and va_l is not None:
        check("va_high >= va_low", va_h >= va_l)
        # Volume inside VA (bucket-level: include prices whose bucket ≤ va_h)
        va_vol = sum(s for p, s in trades if va_l <= (int(p / BUCKET) * BUCKET) <= va_h)
        check(f"va volume >= 70% (got {va_vol/total_vol:.0%})",
              va_vol / total_vol >= 0.7)


# ---------------------------------------------------------------------------
# 4. Regime: balanced
# ---------------------------------------------------------------------------

def test_regime_balanced() -> None:
    print("\n--- 4. Regime: balanced ---")
    sp = SessionProfile()
    # Uniform distribution across 9 price levels — wide VA, last price at centre
    base = 65000.0
    for i in range(-4, 5):
        for _ in range(5):
            sp.ingest_trade(base + i * BUCKET, 5.0, ASIA_TS + (i + 4) * 1_000)
    sp.ingest_trade(base, 1.0, ASIA_TS + 50_000)

    snap = sp.snapshot()
    check("regime = balanced", snap.get("regime") == "balanced",
          f"got {snap.get('regime')!r}")


# ---------------------------------------------------------------------------
# 5. Regime: imbalanced_up (price above VA)
# ---------------------------------------------------------------------------

def test_regime_imbalanced_up() -> None:
    print("\n--- 5. Regime: imbalanced_up ---")
    sp = SessionProfile()
    for _ in range(20):
        sp.ingest_trade(65000.0, 5.0, ASIA_TS)
    # Tiny volume at much higher price — current price is now well above VA
    sp.ingest_trade(66000.0, 0.01, ASIA_TS + 60_000)

    snap = sp.snapshot()
    check("regime = imbalanced_up", snap.get("regime") == "imbalanced_up",
          f"got {snap.get('regime')!r}")


# ---------------------------------------------------------------------------
# 6. Regime: imbalanced_down (price below VA)
# ---------------------------------------------------------------------------

def test_regime_imbalanced_down() -> None:
    print("\n--- 6. Regime: imbalanced_down ---")
    sp = SessionProfile()
    for _ in range(20):
        sp.ingest_trade(65000.0, 5.0, ASIA_TS)
    sp.ingest_trade(64000.0, 0.01, ASIA_TS + 60_000)

    snap = sp.snapshot()
    check("regime = imbalanced_down", snap.get("regime") == "imbalanced_down",
          f"got {snap.get('regime')!r}")


# ---------------------------------------------------------------------------
# 7. reset_if_needed: no reset within same session
# ---------------------------------------------------------------------------

def test_reset_if_needed_same_session() -> None:
    print("\n--- 7. reset_if_needed: same session → False ---")
    sp = SessionProfile()
    sp.ingest_trade(65000.0, 1.0, ASIA_TS)

    result = sp.reset_if_needed(ASIA_TS + 3_600_000)  # +1 h, still Asia
    check("returns False",          result is False)
    check("data preserved",         sp.snapshot().get("session_trade_count") == 1)
    check("session still Asia",     sp.current_session == "Asia")


# ---------------------------------------------------------------------------
# 8. reset_if_needed: reset on session boundary
# ---------------------------------------------------------------------------

def test_reset_if_needed_session_change() -> None:
    print("\n--- 8. reset_if_needed: session change → True ---")
    sp = SessionProfile()
    sp.ingest_trade(65000.0, 1.0, ASIA_TS)

    before_archives = len(sp.get_archived_profiles())
    result = sp.reset_if_needed(LONDON_TS)
    after_archives = len(sp.get_archived_profiles())

    check("returns True",               result is True)
    check("old session archived",       after_archives == before_archives + 1)
    check("current session = London",   sp.current_session == "London")
    check("trade_count cleared",        sp.snapshot().get("session_trade_count") == 0)
    check("volume cleared",             sp.snapshot().get("session_volume") == 0.0)


# ---------------------------------------------------------------------------
# 9. Deterministic replay: same trades → identical snapshot
# ---------------------------------------------------------------------------

def test_deterministic_replay() -> None:
    print("\n--- 9. Deterministic replay ---")
    trades = [
        (64800.0, 0.5), (64850.0, 1.2), (64900.0, 3.0),
        (64950.0, 2.1), (65000.0, 5.0), (65050.0, 1.8), (65100.0, 0.9),
    ]

    def run() -> dict:
        sp = SessionProfile()
        for i, (price, size) in enumerate(trades):
            sp.ingest_trade(price, size, ASIA_TS + i * 1_000)
        return sp.snapshot()

    s1, s2 = run(), run()
    check("poc deterministic",      s1["session_poc"]             == s2["session_poc"])
    check("volume deterministic",   s1["session_volume"]          == s2["session_volume"])
    check("va_high deterministic",  s1["session_value_area_high"] == s2["session_value_area_high"])
    check("va_low deterministic",   s1["session_value_area_low"]  == s2["session_value_area_low"])
    check("regime deterministic",   s1["regime"]                  == s2["regime"])


# ---------------------------------------------------------------------------
# 10. Session boundary mapping
# ---------------------------------------------------------------------------

def test_session_hour_mapping() -> None:
    print("\n--- 10. Session hour → name mapping ---")
    cases = [
        (2,  "Asia"),
        (7,  "Pre-London"),
        (9,  "London"),
        (12, "Pre-NY"),
        (14, "New York"),
        (22, "Pre-Asia"),
    ]
    for hour, expected in cases:
        got = session_name_for_hour(hour)
        check(f"hour {hour:02d}:00 → {expected}", got == expected, f"got {got!r}")


# ---------------------------------------------------------------------------
# 11. UTC transition boundary: Pre-London → London at 08:00
# ---------------------------------------------------------------------------

def test_session_transition_at_08h() -> None:
    print("\n--- 11. UTC boundary: Pre-London → London at 08:00 ---")
    sp = SessionProfile()

    pre_london_ts = ms(2026, 6, 1, 7, 59, 59)   # 1 s before London
    london_start  = ms(2026, 6, 1, 8,  0,  0)   # exact London open

    sp.ingest_trade(65000.0, 1.0, pre_london_ts)
    check("07:59:59 → Pre-London", sp.current_session == "Pre-London")

    switched = sp.reset_if_needed(london_start)
    check("reset at 08:00:00",     switched is True)
    check("new session = London",  sp.current_session == "London")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("  SessionProfile — PR4a Test Suite")
    print("=" * 60)

    test_ingest_trade_basic()
    test_poc_correctness()
    test_value_area_coverage()
    test_regime_balanced()
    test_regime_imbalanced_up()
    test_regime_imbalanced_down()
    test_reset_if_needed_same_session()
    test_reset_if_needed_session_change()
    test_deterministic_replay()
    test_session_hour_mapping()
    test_session_transition_at_08h()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"  RESULT: {PASS} passed, {FAIL} failed  (total {total})")
    print(f"{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
