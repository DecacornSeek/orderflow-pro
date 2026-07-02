"""Session phases + Initial Balance — Sprint B test suite.

Covers: PHASE_DEFS boundary mapping, 24h gap-free coverage, session_phase
in snapshot, initial balance visibility while forming + completion flag.

Run: python tests/test_session_phases.py
"""

import sys
sys.path.insert(0, ".")

from datetime import datetime, timezone

from core.session_profile import (
    SessionProfile, PHASES, PHASE_DEFS,
    phase_name_for_minute, phase_for_ts,
)

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


# ---------------------------------------------------------------------------
# 1. Phase boundary mapping (minute resolution)
# ---------------------------------------------------------------------------

def test_phase_boundaries() -> None:
    print("\n--- 1. Phasen-Grenzen (Minuten-Auflösung) ---")
    cases = [
        (0,  0,  "asia"),
        (6, 59,  "asia"),
        (7,  0,  "london_pre"),
        (8,  0,  "london_open"),
        (9, 59,  "london_open"),
        (10, 0,  "london_session"),
        (12, 0,  "ny_pre"),
        (13, 29, "ny_pre"),
        (13, 30, "ny_open"),
        (15, 29, "ny_open"),
        (15, 30, "overlap"),
        (15, 59, "overlap"),
        (16, 0,  "ny_afternoon"),
        (19, 59, "ny_afternoon"),
        (20, 0,  "asia_pre"),
        (23, 59, "asia_pre"),
    ]
    for hour, minute, expected in cases:
        got = phase_name_for_minute(hour * 60 + minute)
        check(f"{hour:02d}:{minute:02d} UTC -> {expected}", got == expected, f"got {got!r}")


# ---------------------------------------------------------------------------
# 2. 24h coverage: no gaps, no unknown phases
# ---------------------------------------------------------------------------

def test_full_day_coverage() -> None:
    print("\n--- 2. Lückenlose 24h-Abdeckung ---")
    unknown = [m for m in range(24 * 60) if phase_name_for_minute(m) not in PHASES]
    check("alle 1440 Minuten benannt", not unknown, f"unbenannt: {unknown[:5]}")

    # PHASE_DEFS contiguous: each phase ends where the next starts
    contiguous = all(PHASE_DEFS[i][2] == PHASE_DEFS[i + 1][1]
                     for i in range(len(PHASE_DEFS) - 1))
    check("PHASE_DEFS lückenlos", contiguous)
    check("Tag beginnt bei 0",     PHASE_DEFS[0][1] == 0)
    check("Tag endet bei 1440",    PHASE_DEFS[-1][2] == 24 * 60)


# ---------------------------------------------------------------------------
# 3. phase_for_ts
# ---------------------------------------------------------------------------

def test_phase_for_ts() -> None:
    print("\n--- 3. phase_for_ts (Unix ms) ---")
    check("14:00 UTC -> ny_open",   phase_for_ts(ms(2026, 6, 1, 14, 0)) == "ny_open")
    check("02:00 UTC -> asia",      phase_for_ts(ms(2026, 6, 1, 2, 0)) == "asia")
    check("15:45 UTC -> overlap",   phase_for_ts(ms(2026, 6, 1, 15, 45)) == "overlap")


# ---------------------------------------------------------------------------
# 4. session_phase in snapshot (replay-safe: from trade ts, not wall clock)
# ---------------------------------------------------------------------------

def test_snapshot_session_phase() -> None:
    print("\n--- 4. session_phase im Snapshot ---")
    sp = SessionProfile()
    sp.ingest_trade(65000.0, 1.0, ms(2026, 6, 1, 14, 0))   # NY, 14:00
    snap = sp.snapshot()
    check("session_phase = ny_open", snap.get("session_phase") == "ny_open",
          f"got {snap.get('session_phase')!r}")

    sp2 = SessionProfile()
    sp2.ingest_trade(65000.0, 1.0, ms(2026, 6, 1, 2, 30))   # Asia, 02:30
    check("session_phase = asia", sp2.snapshot().get("session_phase") == "asia")


# ---------------------------------------------------------------------------
# 5. Initial balance visible while forming
# ---------------------------------------------------------------------------

def test_ib_while_forming() -> None:
    print("\n--- 5. IB sichtbar während der Bildung ---")
    sp = SessionProfile(initial_balance_minutes=60)
    t0 = ms(2026, 6, 1, 8, 0)      # London open
    sp.ingest_trade(65000.0, 1.0, t0)
    sp.ingest_trade(65200.0, 2.0, t0 + 10 * 60_000)   # +10 min
    sp.ingest_trade(64900.0, 1.5, t0 + 20 * 60_000)   # +20 min

    snap = sp.snapshot()
    check("ib_high vorhanden",        snap.get("initial_balance_high") == 65200.0,
          f"got {snap.get('initial_balance_high')!r}")
    check("ib_low vorhanden",         snap.get("initial_balance_low") == 64900.0)
    check("ib_volume = 4.5",          abs(snap.get("initial_balance_volume", -1) - 4.5) < 1e-9)
    check("ib_complete = False",      snap.get("initial_balance_complete") is False)


# ---------------------------------------------------------------------------
# 6. Initial balance completion after window
# ---------------------------------------------------------------------------

def test_ib_completion() -> None:
    print("\n--- 6. IB abgeschlossen nach 60 min ---")
    sp = SessionProfile(initial_balance_minutes=60)
    t0 = ms(2026, 6, 1, 8, 0)
    sp.ingest_trade(65000.0, 1.0, t0)
    sp.ingest_trade(65300.0, 1.0, t0 + 30 * 60_000)
    # Trade after the IB window closes it
    sp.ingest_trade(65500.0, 1.0, t0 + 61 * 60_000)

    snap = sp.snapshot()
    check("ib_complete = True",     snap.get("initial_balance_complete") is True)
    check("ib_high eingefroren",    snap.get("initial_balance_high") == 65300.0,
          f"got {snap.get('initial_balance_high')!r}")
    check("Post-IB Trade nicht im IB-Volumen",
          abs(snap.get("initial_balance_volume", -1) - 2.0) < 1e-9)


# ---------------------------------------------------------------------------
# 7. IB resets on session change
# ---------------------------------------------------------------------------

def test_ib_reset_on_session_change() -> None:
    print("\n--- 7. IB-Reset bei Session-Wechsel ---")
    sp = SessionProfile()
    sp.ingest_trade(65000.0, 1.0, ms(2026, 6, 1, 8, 0))    # London
    sp.ingest_trade(66000.0, 1.0, ms(2026, 6, 1, 14, 0))   # New York -> reset

    snap = sp.snapshot()
    check("session = New York",   snap.get("session") == "New York")
    check("neues IB-High",        snap.get("initial_balance_high") == 66000.0,
          f"got {snap.get('initial_balance_high')!r}")
    check("neues IB-Volumen",     abs(snap.get("initial_balance_volume", -1) - 1.0) < 1e-9)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("  Session Phases + Initial Balance — Sprint B Test Suite")
    print("=" * 60)

    test_phase_boundaries()
    test_full_day_coverage()
    test_phase_for_ts()
    test_snapshot_session_phase()
    test_ib_while_forming()
    test_ib_completion()
    test_ib_reset_on_session_change()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"  RESULT: {PASS} passed, {FAIL} failed  (total {total})")
    print(f"{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
