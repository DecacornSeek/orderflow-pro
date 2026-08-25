"""profile_structure — Step 5 structure detectors test suite.

Covers: single prints (interior low-volume runs), weak/strong extremes,
double distribution + bridge, structure_context flat features, determinism.

Run: python tests/test_profile_structure.py
"""

import sys
sys.path.insert(0, ".")

from core.profile_structure import (
    find_single_prints, classify_extremes, detect_double_distribution,
    structure_context,
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


# ---------------------------------------------------------------------------
# 1. Single prints: interior low-volume run detected
# ---------------------------------------------------------------------------

def test_single_print_detected() -> None:
    print("\n--- 1. Single Print: innere Niedrig-Volumen-Zone ---")
    # POC volume 100; buckets 65050+65075 at 1% -> single print run
    vap = {
        64950: 40.0, 64975: 80.0, 65000: 100.0, 65025: 60.0,
        65050: 1.0, 65075: 1.0,                       # fast traversal
        65100: 50.0, 65125: 70.0, 65150: 30.0,
    }
    prints = find_single_prints(vap)
    check("genau 1 Zone", len(prints) == 1, f"got {len(prints)}")
    if prints:
        check("price_low = 65050", prints[0]["price_low"] == 65050)
        check("price_high = 65075", prints[0]["price_high"] == 65075)
        check("bucket_count = 2", prints[0]["bucket_count"] == 2)


def test_single_print_edges_excluded() -> None:
    print("\n--- 2. Rand-Buckets sind KEINE Single Prints (Tails) ---")
    vap = {
        64950: 0.5,                                   # low edge tail
        64975: 80.0, 65000: 100.0, 65025: 90.0,
        65050: 0.5,                                   # high edge tail
    }
    prints = find_single_prints(vap)
    check("Tails ignoriert", len(prints) == 0, f"got {prints!r}")


def test_single_print_empty_input() -> None:
    print("\n--- 3. Leerer/kleiner Input -> [] ---")
    check("None -> []", find_single_prints(None) == [])
    check("leer -> []", find_single_prints({}) == [])
    check("2 Buckets -> []", find_single_prints({65000: 10.0, 65025: 1.0}) == [])


# ---------------------------------------------------------------------------
# 4. Extremes: strong (thin tail) vs weak (fat edge)
# ---------------------------------------------------------------------------

def test_strong_high_weak_low() -> None:
    print("\n--- 4. Starkes Hoch (Tail), schwaches Tief (fett) ---")
    # Body avg ~50; top edge ~1 (ratio 0.02 -> strong);
    # bottom edge ~50 (ratio ~1.0 -> weak)
    vap = {
        64900: 50.0, 64925: 52.0,          # fat low edge -> weak
        64950: 48.0, 64975: 55.0, 65000: 50.0, 65025: 47.0,
        65050: 1.0, 65075: 1.0,            # thin high edge -> strong
    }
    ex = classify_extremes(vap)
    check("high = strong", ex["high_strength"] == "strong", f"got {ex['high_strength']!r}")
    check("low = weak", ex["low_strength"] == "weak", f"got {ex['low_strength']!r}")
    check("ratios vorhanden", ex["high_edge_ratio"] is not None and ex["low_edge_ratio"] is not None)


def test_extremes_too_small() -> None:
    print("\n--- 5. Zu kleines Profil -> unknown ---")
    ex = classify_extremes({65000: 10.0, 65025: 5.0, 65050: 2.0})
    check("high unknown", ex["high_strength"] == "unknown")
    check("low unknown", ex["low_strength"] == "unknown")
    check("None input", classify_extremes(None)["high_strength"] == "unknown")


# ---------------------------------------------------------------------------
# 6. Double distribution
# ---------------------------------------------------------------------------

def test_double_distribution() -> None:
    print("\n--- 6. Double Distribution mit Bruecke ---")
    # Two acceptance areas separated by a low-volume bridge
    vap = {
        64900: 30.0, 64925: 90.0, 64950: 100.0, 64975: 85.0, 65000: 25.0,
        65025: 1.0, 65050: 1.0,                                # bridge
        65075: 30.0, 65100: 88.0, 65125: 95.0, 65150: 80.0, 65175: 20.0,
    }
    dd = detect_double_distribution(vap)
    check("erkannt", dd is not None)
    if dd:
        check("upper > lower", dd["upper_poc"] > dd["lower_poc"])
        check("bruecke gefunden", dd["bridge"] is not None)
        if dd["bridge"]:
            check("bruecke zwischen POCs",
                  dd["lower_poc"] < dd["bridge"]["price_low"]
                  and dd["bridge"]["price_high"] < dd["upper_poc"])


def test_single_distribution_none() -> None:
    print("\n--- 7. Normale D-Verteilung -> None ---")
    vap = {64950: 20.0, 64975: 60.0, 65000: 100.0, 65025: 55.0, 65050: 15.0}
    check("D-Profil -> None", detect_double_distribution(vap) is None)
    check("None input -> None", detect_double_distribution(None) is None)


# ---------------------------------------------------------------------------
# 8. structure_context: flat feature row
# ---------------------------------------------------------------------------

def test_structure_context() -> None:
    print("\n--- 8. structure_context Feature-Kontrakt ---")
    vap = {
        64900: 50.0, 64925: 52.0, 64950: 48.0, 64975: 55.0,
        65000: 50.0, 65025: 1.0, 65050: 47.0, 65075: 45.0,
    }
    ctx = structure_context(vap)
    for key in ("n_single_prints", "single_prints", "high_strength",
                "low_strength", "is_double_distribution",
                "dist_upper_poc", "dist_lower_poc"):
        check(f"key '{key}' vorhanden", key in ctx)
    check("n_single_prints = len(list)",
          ctx["n_single_prints"] == len(ctx["single_prints"]))


def test_deterministic() -> None:
    print("\n--- 9. Deterministisch ---")
    vap = {64900 + i * 25: float((i * 7) % 13 + 1) for i in range(12)}
    check("structure_context stabil", structure_context(vap) == structure_context(vap))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("  profile_structure — Step 5 Test Suite")
    print("=" * 60)

    test_single_print_detected()
    test_single_print_edges_excluded()
    test_single_print_empty_input()
    test_strong_high_weak_low()
    test_extremes_too_small()
    test_double_distribution()
    test_single_distribution_none()
    test_structure_context()
    test_deterministic()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"  RESULT: {PASS} passed, {FAIL} failed  (total {total})")
    print(f"{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
