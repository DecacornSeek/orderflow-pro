"""road_map — Step 7 road map + setup matrix test suite.

Covers: day_type rules (all regime combinations), setup matrix, A->B path
per direction, zone speed expectations, missing-input robustness, determinism.

Run: python tests/test_road_map.py
"""

import sys
sys.path.insert(0, ".")

from core.road_map import build_road_map, SETUP_MATRIX, _day_type

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


def zone(kind: str, low: int, high: int) -> dict:
    return {"price_low": low, "price_high": high, "kind": kind,
            "volume": 10.0, "recurrence": 1, "state": "untested", "sources": []}


ZONES = {
    "zone_at": zone("poc", 65000, 65025),
    "zone_below": zone("hvn", 64800, 64900),
    "zone_above": zone("single_print", 65100, 65150),
}


# ---------------------------------------------------------------------------
# 1. day_type rules — all regime combinations
# ---------------------------------------------------------------------------

def test_day_type_rules() -> None:
    print("\n--- 1. day_type Regeln ---")
    cases = [
        ("balanced",        "imbalanced_up",   "balance"),
        ("balanced",        None,               "balance"),
        ("imbalanced_up",   "imbalanced_up",   "trend_up"),
        ("imbalanced_up",   "balanced",        "trend_up"),
        ("imbalanced_up",   "imbalanced_down", "conflicted"),
        ("imbalanced_down", "imbalanced_down", "trend_down"),
        ("imbalanced_down", "neutral",         "trend_down"),
        ("imbalanced_down", "imbalanced_up",   "conflicted"),
        ("imbalanced",      "balanced",        "transition"),
        ("neutral",         "balanced",        "neutral"),
        (None,              None,               "neutral"),
    ]
    for s, w, expected in cases:
        got = _day_type(s, w)
        check(f"session={s!r} weekly={w!r} -> {expected}", got == expected,
              f"got {got!r}")


# ---------------------------------------------------------------------------
# 2. Setup matrix per day type
# ---------------------------------------------------------------------------

def test_setup_matrix() -> None:
    print("\n--- 2. Setup-Matrix ---")
    rm = build_road_map({"regime": "balanced"}, {"regime": "neutral"}, ZONES)
    check("balance -> fade_edge_counter", rm["allowed_setups"] == ["fade_edge_counter"])

    rm = build_road_map({"regime": "imbalanced_up"}, {"regime": "imbalanced_up"}, ZONES)
    check("trend_up -> continuation+pullback long",
          rm["allowed_setups"] == ["breakout_continuation_long", "pullback_to_value_long"])

    rm = build_road_map({"regime": "imbalanced_up"}, {"regime": "imbalanced_down"}, ZONES)
    check("conflicted -> stand_aside", rm["allowed_setups"] == ["stand_aside"])

    check("Matrix deckt alle day_types", set(SETUP_MATRIX.keys()) ==
          {"balance", "trend_up", "trend_down", "transition", "conflicted", "neutral"})


# ---------------------------------------------------------------------------
# 3. Point A -> Point B per direction
# ---------------------------------------------------------------------------

def test_path_trend_up() -> None:
    print("\n--- 3. Pfad bei trend_up: A = at/below, B = above ---")
    rm = build_road_map({"regime": "imbalanced_up"}, {"regime": "balanced"}, ZONES)
    check("direction = up", rm["dominant_direction"] == "up")
    check("point_a = zone_at", rm["point_a"] == ZONES["zone_at"])
    check("point_b = zone_above", rm["point_b"] == ZONES["zone_above"])


def test_path_trend_down() -> None:
    print("\n--- 4. Pfad bei trend_down: A = at/above, B = below ---")
    rm = build_road_map({"regime": "imbalanced_down"}, {"regime": "balanced"}, ZONES)
    check("direction = down", rm["dominant_direction"] == "down")
    check("point_a = zone_at", rm["point_a"] == ZONES["zone_at"])
    check("point_b = zone_below", rm["point_b"] == ZONES["zone_below"])

    # Ohne zone_at: A = zone_above (Gegenrichtung)
    z = dict(ZONES, zone_at=None)
    rm2 = build_road_map({"regime": "imbalanced_down"}, {"regime": "balanced"}, z)
    check("ohne at: point_a = zone_above", rm2["point_a"] == ZONES["zone_above"])


def test_path_balance() -> None:
    print("\n--- 5. Pfad bei balance: Range-Kanten ---")
    rm = build_road_map({"regime": "balanced"}, {"regime": "balanced"}, ZONES)
    check("direction = none", rm["dominant_direction"] == "none")
    check("point_a = zone_below", rm["point_a"] == ZONES["zone_below"])
    check("point_b = zone_above", rm["point_b"] == ZONES["zone_above"])


# ---------------------------------------------------------------------------
# 6. Zone speed expectations
# ---------------------------------------------------------------------------

def test_zone_speeds() -> None:
    print("\n--- 6. Erwartete Geschwindigkeit pro Zonen-Art ---")
    rm = build_road_map({"regime": "balanced"}, {"regime": "balanced"}, ZONES)
    check("single_print oben -> fast", rm["expected_speed_above"] == "fast")
    check("hvn unten -> rotation", rm["expected_speed_below"] == "rotation")

    z = {"zone_at": None, "zone_below": zone("val", 64900, 64925),
         "zone_above": zone("vah", 65100, 65125)}
    rm2 = build_road_map({"regime": "balanced"}, {"regime": "balanced"}, z)
    check("vah -> reaction", rm2["expected_speed_above"] == "reaction")
    check("val -> reaction", rm2["expected_speed_below"] == "reaction")


# ---------------------------------------------------------------------------
# 7. Robustness: missing inputs never crash
# ---------------------------------------------------------------------------

def test_missing_inputs() -> None:
    print("\n--- 7. Fehlende Inputs -> neutral, kein Crash ---")
    rm = build_road_map(None, None, None)
    check("day_type = neutral", rm["day_type"] == "neutral")
    check("stand_aside", rm["allowed_setups"] == ["stand_aside"])
    check("kein point_a", rm["point_a"] is None)
    check("keine speeds", rm["expected_speed_above"] is None
          and rm["expected_speed_below"] is None)

    rm2 = build_road_map({"session": "N/A"}, {"week": "N/A"}, {})
    check("N/A Kontexte -> neutral", rm2["day_type"] == "neutral")


def test_deterministic() -> None:
    print("\n--- 8. Deterministisch ---")
    a = build_road_map({"regime": "imbalanced_up"}, {"regime": "balanced"}, ZONES, 65010.0)
    b = build_road_map({"regime": "imbalanced_up"}, {"regime": "balanced"}, ZONES, 65010.0)
    check("gleicher Kontext -> gleicher Plan", a == b)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("  road_map — Step 7 Test Suite")
    print("=" * 60)

    test_day_type_rules()
    test_setup_matrix()
    test_path_trend_up()
    test_path_trend_down()
    test_path_balance()
    test_zone_speeds()
    test_missing_inputs()
    test_deterministic()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"  RESULT: {PASS} passed, {FAIL} failed  (total {total})")
    print(f"{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
