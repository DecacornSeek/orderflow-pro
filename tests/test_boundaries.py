"""PR4a Evidence 3 — Explicit UTC boundary tests.

Tests two critical time boundaries:
  A) Session: Pre-Asia -> Asia at 00:00 UTC  (midnight crossing)
  B) Weekly:  old week -> new week at Sunday 22:00:00 UTC (crypto anchor)

Each boundary is tested at three points: 1 second before, exact crossing,
1 second after.

Run: python tests/test_boundaries.py
"""

import sys
sys.path.insert(0, ".")

from datetime import datetime, timezone

from core.session_profile import SessionProfile, session_name_for_hour
from core.weekly_profile import WeeklyProfile, _week_start_ms

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        suffix = f"  ({detail})" if detail else ""
        print(f"  [FAIL] {name}{suffix}")


def ms(year: int, month: int, day: int,
       hour: int, minute: int = 0, second: int = 0) -> int:
    return int(
        datetime(year, month, day, hour, minute, second,
                 tzinfo=timezone.utc).timestamp() * 1000
    )


# ============================================================================
# A. Session boundary: Pre-Asia → Asia at 00:00 UTC
#
# SESSION_DEFS mapping:
#   hour 23 → Pre-Asia  (start=21, end=24)
#   hour  0 → Asia      (start=0,  end=8)
#
# Test timeline  (2026-06-01 / 2026-06-02):
#   23:59:59 UTC  → Pre-Asia
#   00:00:00 UTC  → Asia        ← exact boundary
#   00:00:01 UTC  → Asia
# ============================================================================

TS_2359 = ms(2026, 6, 1, 23, 59, 59)   # Pre-Asia, 1 s before midnight
TS_0000 = ms(2026, 6, 2,  0,  0,  0)   # Asia, exact midnight
TS_0001 = ms(2026, 6, 2,  0,  0,  1)   # Asia, 1 s after midnight


def test_session_hour_at_boundary() -> None:
    print("\n--- A1. Hour mapping at 23:00 / 00:00 ---")
    check("hour 23 -> Pre-Asia", session_name_for_hour(23) == "Pre-Asia",
          f"got {session_name_for_hour(23)!r}")
    check("hour  0 -> Asia",     session_name_for_hour(0)  == "Asia",
          f"got {session_name_for_hour(0)!r}")


def test_session_ingest_before_midnight() -> None:
    print("\n--- A2. Ingest at 23:59:59 UTC -> Pre-Asia ---")
    sp = SessionProfile()
    sp.ingest_trade(65000.0, 1.0, TS_2359)
    check("session = Pre-Asia", sp.current_session == "Pre-Asia",
          f"got {sp.current_session!r}")
    snap = sp.snapshot()
    check("regime present",     "regime" in snap)
    check("volume = 1.0",       abs(snap.get("session_volume", -1) - 1.0) < 1e-9)


def test_session_reset_at_exact_midnight() -> None:
    print("\n--- A3. reset_if_needed at exactly 00:00:00 UTC ---")
    sp = SessionProfile()
    sp.ingest_trade(65000.0, 1.0, TS_2359)
    check("before: Pre-Asia",          sp.current_session == "Pre-Asia")

    switched = sp.reset_if_needed(TS_0000)
    check("reset triggered",           switched is True)
    check("after: Asia",               sp.current_session == "Asia",
          f"got {sp.current_session!r}")
    check("Pre-Asia archived",         len(sp.get_archived_profiles()) == 1)
    check("archived label = Pre-Asia", sp.get_archived_profiles()[0].label == "Pre-Asia")
    check("fresh session volume = 0",  sp.snapshot().get("session_volume") == 0.0)


def test_session_ingest_straddles_midnight() -> None:
    """Trade at 23:59:59 then trade at 00:00:01 — auto-reset via ingest."""
    print("\n--- A4. ingest straddles midnight (auto-reset) ---")
    sp = SessionProfile()
    sp.ingest_trade(65000.0, 2.0, TS_2359)
    sp.ingest_trade(65100.0, 3.0, TS_0001)   # crosses midnight inside ingest()

    check("current session = Asia",        sp.current_session == "Asia",
          f"got {sp.current_session!r}")
    check("Asia has only the post-midnight trade",
          abs(sp.snapshot().get("session_volume", -1) - 3.0) < 1e-9)
    check("Pre-Asia was archived",         len(sp.get_archived_profiles()) == 1)
    check("archived volume = 2.0",
          abs(sp.get_archived_profiles()[0].total_volume - 2.0) < 1e-9)


def test_session_reset_idempotent_after_crossing() -> None:
    """Calling reset_if_needed with a time already in the new session returns False."""
    print("\n--- A5. reset_if_needed is idempotent once in new session ---")
    sp = SessionProfile()
    sp.ingest_trade(65000.0, 1.0, TS_2359)
    sp.reset_if_needed(TS_0000)          # first crossing
    second_result = sp.reset_if_needed(TS_0001)  # 1 s later, same session
    check("second reset_if_needed returns False", second_result is False)
    check("session still Asia",           sp.current_session == "Asia")
    check("archive count unchanged",      len(sp.get_archived_profiles()) == 1)


# ============================================================================
# B. Weekly boundary: Sunday 22:00:00 UTC (crypto week anchor)
#
# Calendar:  June 7, 2026 is a Sunday.
# Boundary timestamps:
#   2026-06-07 21:59:59 UTC  → old week (started May 31, 22:00 UTC)
#   2026-06-07 22:00:00 UTC  → new week  ← exact anchor
#   2026-06-07 22:00:01 UTC  → same new week
# ============================================================================

TS_W_BEFORE  = ms(2026, 6, 7, 21, 59, 59)   # still old week
TS_W_EXACTLY = ms(2026, 6, 7, 22,  0,  0)   # exact anchor = new week
TS_W_AFTER   = ms(2026, 6, 7, 22,  0,  1)   # 1 s into new week


def test_week_label_before_anchor() -> None:
    print("\n--- B1. _week_start_ms at 21:59:59 -> old week ---")
    ws_ms, label = _week_start_ms(TS_W_BEFORE)
    ws_dt = datetime.fromtimestamp(ws_ms / 1000, tz=timezone.utc)
    check("start is Sunday", ws_dt.weekday() == 6, f"weekday={ws_dt.weekday()}")
    check("start is 22:00",  ws_dt.hour == 22,     f"hour={ws_dt.hour}")
    # Week that started May 31 at 22:00 UTC
    check("start is May 31", ws_dt.day == 31 and ws_dt.month == 5,
          f"got {ws_dt.date()}")


def test_week_label_at_exact_anchor() -> None:
    print("\n--- B2. _week_start_ms at 22:00:00 -> new week starts ---")
    ws_ms, label = _week_start_ms(TS_W_EXACTLY)
    ws_dt = datetime.fromtimestamp(ws_ms / 1000, tz=timezone.utc)
    check("start is Sunday",  ws_dt.weekday() == 6)
    check("start is June 7",  ws_dt.day == 7 and ws_dt.month == 6,
          f"got {ws_dt.date()}")
    check("start is 22:00",   ws_dt.hour == 22)


def test_week_labels_differ_across_anchor() -> None:
    print("\n--- B3. Labels differ across 22:00 anchor ---")
    _, l_before  = _week_start_ms(TS_W_BEFORE)
    _, l_exactly = _week_start_ms(TS_W_EXACTLY)
    _, l_after   = _week_start_ms(TS_W_AFTER)
    check("before != at_anchor",  l_before != l_exactly,
          f"{l_before!r} vs {l_exactly!r}")
    check("at_anchor == 1s after", l_exactly == l_after,
          f"{l_exactly!r} vs {l_after!r}")


def test_weekly_reset_at_exact_anchor() -> None:
    print("\n--- B4. WeeklyProfile.reset_if_needed at 22:00:00 UTC ---")
    wp = WeeklyProfile()
    wp.ingest_trade(65000.0, 5.0, TS_W_BEFORE)
    old_label = wp._label

    switched = wp.reset_if_needed(TS_W_EXACTLY)
    check("reset triggered",                switched is True)
    check("label changed",                  wp._label != old_label,
          f"old={old_label!r} new={wp._label!r}")
    check("old week archived",              len(wp.get_archived_profiles()) == 1)
    check("archived volume = 5.0",
          abs(wp.get_archived_profiles()[0].total_volume - 5.0) < 1e-9)
    check("new week volume = 0",            wp.snapshot().get("week_volume") == 0.0)
    check("new week poc = None",            wp.snapshot().get("week_poc") is None)


def test_weekly_ingest_straddles_anchor() -> None:
    """Trade before anchor then trade after — auto-reset inside ingest."""
    print("\n--- B5. ingest straddles 22:00 anchor (auto-reset) ---")
    wp = WeeklyProfile()
    wp.ingest_trade(65000.0, 3.0, TS_W_BEFORE)
    wp.ingest_trade(65200.0, 7.0, TS_W_AFTER)   # crosses anchor inside ingest()

    check("current week = new label",
          wp._label != _week_start_ms(TS_W_BEFORE)[1],
          f"label={wp._label!r}")
    check("new week volume = 7.0",
          abs(wp.snapshot().get("week_volume", -1) - 7.0) < 1e-9)
    check("old week archived",        len(wp.get_archived_profiles()) == 1)
    check("archived volume = 3.0",
          abs(wp.get_archived_profiles()[0].total_volume - 3.0) < 1e-9)


def test_weekly_reset_idempotent() -> None:
    print("\n--- B6. reset_if_needed idempotent once in new week ---")
    wp = WeeklyProfile()
    wp.ingest_trade(65000.0, 1.0, TS_W_BEFORE)
    wp.reset_if_needed(TS_W_EXACTLY)         # first crossing
    r2 = wp.reset_if_needed(TS_W_AFTER)     # 1 s later, same new week
    check("second reset returns False", r2 is False)
    check("archive count still 1",      len(wp.get_archived_profiles()) == 1)


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    print("=" * 60)
    print("  PR4a Boundary Tests — UTC edges")
    print("=" * 60)

    # Session (midnight) boundary
    test_session_hour_at_boundary()
    test_session_ingest_before_midnight()
    test_session_reset_at_exact_midnight()
    test_session_ingest_straddles_midnight()
    test_session_reset_idempotent_after_crossing()

    # Weekly (Sunday 22:00) boundary
    test_week_label_before_anchor()
    test_week_label_at_exact_anchor()
    test_week_labels_differ_across_anchor()
    test_weekly_reset_at_exact_anchor()
    test_weekly_ingest_straddles_anchor()
    test_weekly_reset_idempotent()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"  RESULT: {PASS} passed, {FAIL} failed  (total {total})")
    print(f"{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
