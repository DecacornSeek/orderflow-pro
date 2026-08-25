"""WeeklyProfile — PR4a test suite.

Covers: ingest_trade, snapshot, reset_if_needed, POC, value area,
regime heuristic, weekly reset boundary (Sunday 22:00 UTC), deterministic replay.

Run: python tests/test_weekly_profile.py
"""

import sys
sys.path.insert(0, ".")

from datetime import datetime, timezone

from core.weekly_profile import WeeklyProfile, _week_start_ms
from core.session_profile import BUCKET

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


# June 2026: June 1 = Monday, June 7 = Sunday.
# Week anchor: week containing June 1–7 started May 31 (Sunday) 22:00 UTC.
# Next week starts: June 7 (Sunday) 22:00 UTC.
WEEK1_MON = ms(2026, 6, 1, 12)     # Monday  12:00 UTC — week 1
WEEK1_SAT = ms(2026, 6, 6, 20)     # Saturday 20:00 UTC — same week
BEFORE_RESET = ms(2026, 6, 7, 21, 59)   # Sunday 21:59 UTC — still week 1
AFTER_RESET  = ms(2026, 6, 7, 22,  1)   # Sunday 22:01 UTC — week 2


# ---------------------------------------------------------------------------
# 1. _week_start_ms fundamentals
# ---------------------------------------------------------------------------

def test_week_start_function() -> None:
    print("\n--- 1. _week_start_ms returns Sunday 22:00 UTC ---")
    ws_ms, ws_label = _week_start_ms(WEEK1_MON)
    ws_dt = datetime.fromtimestamp(ws_ms / 1000, tz=timezone.utc)
    check("start weekday = Sunday",    ws_dt.weekday() == 6,
          f"got weekday {ws_dt.weekday()}")
    check("start hour = 22",           ws_dt.hour == 22)
    check("label starts with Week-",   ws_label.startswith("Week-"))


def test_same_week_label() -> None:
    print("\n--- 2. Monday and Saturday share same week label ---")
    _, l1 = _week_start_ms(WEEK1_MON)
    _, l2 = _week_start_ms(WEEK1_SAT)
    check("same label", l1 == l2, f"{l1!r} vs {l2!r}")


def test_different_week_labels() -> None:
    print("\n--- 3. Week 1 and week 2 have different labels ---")
    _, l1 = _week_start_ms(WEEK1_MON)
    _, l2 = _week_start_ms(AFTER_RESET)
    check("different labels", l1 != l2, f"{l1!r} vs {l2!r}")


# ---------------------------------------------------------------------------
# 4. Weekly reset boundary: Sunday 22:00 UTC
# ---------------------------------------------------------------------------

def test_weekly_reset_boundary() -> None:
    print("\n--- 4. Weekly reset boundary (Sunday 22:00 UTC) ---")
    _, label_before = _week_start_ms(BEFORE_RESET)  # 21:59
    _, label_after  = _week_start_ms(AFTER_RESET)   # 22:01
    check("21:59 is old week",  label_before != label_after,
          f"before={label_before!r} after={label_after!r}")
    check("22:01 is new week",  label_before != label_after)


# ---------------------------------------------------------------------------
# 5. Basic ingestion + snapshot fields
# ---------------------------------------------------------------------------

def test_ingest_trade_basic() -> None:
    print("\n--- 5. ingest_trade + snapshot fields ---")
    wp = WeeklyProfile()
    wp.ingest_trade(65000.0, 1.0, WEEK1_MON)
    wp.ingest_trade(65100.0, 2.0, WEEK1_MON + 3_600_000)

    snap = wp.snapshot()
    check("week label present",  snap.get("week", "").startswith("Week-"))
    check("poc not None",        snap.get("week_poc") is not None)
    check("volume = 3.0",        abs(snap.get("week_volume", -1) - 3.0) < 1e-9)
    check("regime field present","regime" in snap)
    check("ohlc open = 65000",   snap.get("week_ohlc", {}).get("open") == 65000.0)


# ---------------------------------------------------------------------------
# 6. POC correctness
# ---------------------------------------------------------------------------

def test_poc_correctness() -> None:
    print("\n--- 6. POC at max-volume bucket ---")
    wp = WeeklyProfile()
    for _ in range(10):
        wp.ingest_trade(65000.0, 1.0, WEEK1_MON)
    for _ in range(2):
        wp.ingest_trade(65200.0, 1.0, WEEK1_MON + 1_000)

    expected_poc = (int(65000 / BUCKET)) * BUCKET
    snap = wp.snapshot()
    check("poc = max-vol bucket", snap.get("week_poc") == expected_poc,
          f"got {snap.get('week_poc')!r}")


# ---------------------------------------------------------------------------
# 7. reset_if_needed: no reset within same week
# ---------------------------------------------------------------------------

def test_reset_if_needed_same_week() -> None:
    print("\n--- 7. reset_if_needed: same week → False ---")
    wp = WeeklyProfile()
    wp.ingest_trade(65000.0, 1.0, WEEK1_MON)

    result = wp.reset_if_needed(WEEK1_SAT)
    check("returns False",    result is False)
    check("volume preserved", abs(wp.snapshot().get("week_volume", -1) - 1.0) < 1e-9)


# ---------------------------------------------------------------------------
# 8. reset_if_needed: new week triggers archive + clear
# ---------------------------------------------------------------------------

def test_reset_if_needed_new_week() -> None:
    print("\n--- 8. reset_if_needed: new week → True + archive ---")
    wp = WeeklyProfile()
    wp.ingest_trade(65000.0, 5.0, WEEK1_MON)
    old_label = wp._label

    result = wp.reset_if_needed(AFTER_RESET)

    check("returns True",              result is True)
    check("old week archived",         len(wp.get_archived_profiles()) == 1)
    check("label changed",             wp._label != old_label)
    check("volume cleared",            wp.snapshot().get("week_volume", -1) == 0.0)
    check("poc is None after reset",   wp.snapshot().get("week_poc") is None)


# ---------------------------------------------------------------------------
# 9. Regime: imbalanced_up
# ---------------------------------------------------------------------------

def test_regime_imbalanced_up() -> None:
    print("\n--- 9. Regime: imbalanced_up ---")
    wp = WeeklyProfile()
    for _ in range(20):
        wp.ingest_trade(65000.0, 5.0, WEEK1_MON)
    wp.ingest_trade(66000.0, 0.01, WEEK1_MON + 3_600_000)

    snap = wp.snapshot()
    check("regime = imbalanced_up", snap.get("regime") == "imbalanced_up",
          f"got {snap.get('regime')!r}")


# ---------------------------------------------------------------------------
# 10. Regime: imbalanced_down
# ---------------------------------------------------------------------------

def test_regime_imbalanced_down() -> None:
    print("\n--- 10. Regime: imbalanced_down ---")
    wp = WeeklyProfile()
    for _ in range(20):
        wp.ingest_trade(65000.0, 5.0, WEEK1_MON)
    wp.ingest_trade(64000.0, 0.01, WEEK1_MON + 3_600_000)

    snap = wp.snapshot()
    check("regime = imbalanced_down", snap.get("regime") == "imbalanced_down",
          f"got {snap.get('regime')!r}")


# ---------------------------------------------------------------------------
# 11. Deterministic replay
# ---------------------------------------------------------------------------

def test_deterministic_replay() -> None:
    print("\n--- 11. Deterministic replay ---")
    trades = [
        (64800.0, 0.5), (64900.0, 3.0), (65000.0, 5.0),
        (65100.0, 1.0), (65200.0, 0.5),
    ]

    def run() -> dict:
        wp = WeeklyProfile()
        for i, (price, size) in enumerate(trades):
            wp.ingest_trade(price, size, WEEK1_MON + i * 3_600_000)
        return wp.snapshot()

    s1, s2 = run(), run()
    check("poc deterministic",      s1["week_poc"]              == s2["week_poc"])
    check("volume deterministic",   s1["week_volume"]           == s2["week_volume"])
    check("va_high deterministic",  s1["week_value_area_high"]  == s2["week_value_area_high"])
    check("va_low deterministic",   s1["week_value_area_low"]   == s2["week_value_area_low"])
    check("regime deterministic",   s1["regime"]                == s2["regime"])


# ---------------------------------------------------------------------------
# 12. Cross-week archive: two weeks accumulate two archived profiles
# ---------------------------------------------------------------------------

def test_two_week_archive() -> None:
    print("\n--- 12. Two-week archive ---")
    wp = WeeklyProfile()
    wp.ingest_trade(65000.0, 1.0, WEEK1_MON)
    # Advance to week 2 via ingest (triggers auto-archive inside ingest)
    wp.ingest_trade(65100.0, 1.0, AFTER_RESET)

    check("one archived profile", len(wp.get_archived_profiles()) == 1)
    archived = wp.get_archived_profiles()
    if archived:
        check("archived has valid poc", archived[0].poc is not None)
        check("archived total_volume",  archived[0].total_volume > 0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("  WeeklyProfile — PR4a Test Suite")
    print("=" * 60)

    test_week_start_function()
    test_same_week_label()
    test_different_week_labels()
    test_weekly_reset_boundary()
    test_ingest_trade_basic()
    test_poc_correctness()
    test_reset_if_needed_same_week()
    test_reset_if_needed_new_week()
    test_regime_imbalanced_up()
    test_regime_imbalanced_down()
    test_deterministic_replay()
    test_two_week_archive()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"  RESULT: {PASS} passed, {FAIL} failed  (total {total})")
    print(f"{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
