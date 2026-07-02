"""business_zones — Step 6 zone registry test suite.

Covers: zone derivation from ProfileSnapshots, merge + recurrence,
nearest zones (A->B path), tested/repaired state machine, snapshot contract,
determinism.

Run: python tests/test_business_zones.py
"""

import sys
sys.path.insert(0, ".")

from core.volume_profile import BUCKET, ProfileSnapshot
from core.business_zones import Zone, build_zones, nearest_zones, ZoneRegistry

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


def make_profile(label: str, vap: dict, poc=None, vah=None, val=None) -> ProfileSnapshot:
    prices = sorted(vap.keys())
    return ProfileSnapshot(
        label=label, timestamp=1780000000000,
        ohlc={"open": float(prices[0]), "high": float(prices[-1]),
              "low": float(prices[0]), "close": float(prices[-1])},
        poc=poc, value_area_high=vah, value_area_low=val,
        total_volume=sum(vap.values()), bucket_count=len(vap),
        vap=dict(vap), poc_drift=[],
    )


# Standard-Profil: POC 65000, VA 64950-65050, single print bei 65100-65125
STD_VAP = {
    64900: 10.0, 64925: 30.0, 64950: 60.0, 64975: 85.0,
    65000: 100.0, 65025: 80.0, 65050: 55.0, 65075: 25.0,
    65100: 1.0, 65125: 1.0,
    65150: 20.0, 65175: 8.0,
}


def std_profile(label="London") -> ProfileSnapshot:
    return make_profile(label, STD_VAP, poc=65000, vah=65050, val=64950)


# ---------------------------------------------------------------------------
# 1. Zone derivation
# ---------------------------------------------------------------------------

def test_zone_derivation() -> None:
    print("\n--- 1. Zonen aus einem Profil ---")
    zones = build_zones([std_profile()])
    kinds = {z.kind for z in zones}
    check("poc-Zone", "poc" in kinds)
    check("vah-Zone", "vah" in kinds)
    check("val-Zone", "val" in kinds)
    check("hvn-Zone", "hvn" in kinds)
    check("single_print-Zone", "single_print" in kinds)

    sp = [z for z in zones if z.kind == "single_print"]
    check("single print bei 65100", sp and sp[0].price_low == 65100,
          f"got {sp[0].price_low if sp else None!r}")
    poc = [z for z in zones if z.kind == "poc"][0]
    check("poc-Zone = 1 Bucket breit", poc.price_high - poc.price_low == BUCKET)


# ---------------------------------------------------------------------------
# 2. Merge + recurrence: same POC in two profiles -> one stronger zone
# ---------------------------------------------------------------------------

def test_merge_recurrence() -> None:
    print("\n--- 2. Recurrence: 2 Profile, gleicher POC -> 1 Zone ---")
    zones = build_zones([std_profile("London"), std_profile("New York")])
    pocs = [z for z in zones if z.kind == "poc"]
    check("genau 1 poc-Zone", len(pocs) == 1, f"got {len(pocs)}")
    if pocs:
        check("recurrence = 2", pocs[0].recurrence == 2, f"got {pocs[0].recurrence}")
        check("beide Quellen", set(pocs[0].source_labels) == {"London", "New York"})
    check("staerkste Zone zuerst", zones[0].recurrence == 2)


def test_distinct_pocs_not_merged() -> None:
    print("\n--- 3. Weit entfernte POCs bleiben getrennt ---")
    vap2 = {k + 500: v for k, v in STD_VAP.items()}
    p2 = make_profile("Asia", vap2, poc=65500, vah=65550, val=65450)
    zones = build_zones([std_profile(), p2])
    pocs = [z for z in zones if z.kind == "poc"]
    check("2 poc-Zonen", len(pocs) == 2, f"got {len(pocs)}")


# ---------------------------------------------------------------------------
# 4. Nearest zones: Point A -> Point B
# ---------------------------------------------------------------------------

def test_nearest_zones() -> None:
    print("\n--- 4. Nearest Zones (A -> B Pfad) ---")
    zones = build_zones([std_profile()])
    # Preis zwischen VAH-Zone (65050-65075) und single print (65100-65150)
    res = nearest_zones(zones, 65090.0)
    check("zone_below vorhanden", res["zone_below"] is not None)
    check("zone_above vorhanden", res["zone_above"] is not None)
    if res["zone_below"] and res["zone_above"]:
        check("below endet unter Preis", res["zone_below"]["price_high"] < 65090)
        check("above beginnt ueber Preis", res["zone_above"]["price_low"] > 65090)

    # Preis IM POC
    res2 = nearest_zones(zones, 65010.0)
    check("zone_at im POC", res2["zone_at"] is not None
          and res2["zone_at"]["price_low"] <= 65010 <= res2["zone_at"]["price_high"])


# ---------------------------------------------------------------------------
# 5. State machine: untested -> tested -> repaired
# ---------------------------------------------------------------------------

def test_state_machine() -> None:
    print("\n--- 5. Zustand: untested -> tested -> repaired ---")
    reg = ZoneRegistry()
    reg.rebuild([std_profile()])

    sp = [z for z in reg._zones if z.kind == "single_print"][0]
    check("start: untested", sp.state == "untested")

    # Preis beruehrt die Zone (Segment endet in ihr) -> tested
    reg.update_price(65000.0)
    reg.update_price(65110.0)
    check("beruehrt -> tested", sp.state == "tested", f"got {sp.state!r}")

    # Preis durchquert die GESAMTE Zone -> repaired
    reg.update_price(65200.0)
    check("durchquert -> repaired", sp.state == "repaired", f"got {sp.state!r}")

    # POC-Zone: durchqueren macht nur tested, nie repaired
    poc = [z for z in reg._zones if z.kind == "poc"][0]
    check("poc nur tested", poc.state == "tested", f"got {poc.state!r}")


def test_state_survives_rebuild() -> None:
    print("\n--- 6. Zustand ueberlebt rebuild ---")
    reg = ZoneRegistry()
    reg.rebuild([std_profile()])
    reg.update_price(65000.0)
    reg.update_price(65200.0)   # repariert den single print
    sp_state = [z for z in reg._zones if z.kind == "single_print"][0].state

    reg.rebuild([std_profile()])   # gleiche Profile -> gleiche Zonen
    sp2 = [z for z in reg._zones if z.kind == "single_print"][0]
    check("repaired bleibt", sp2.state == sp_state == "repaired",
          f"got {sp2.state!r}")


# ---------------------------------------------------------------------------
# 7. Snapshot contract
# ---------------------------------------------------------------------------

def test_snapshot_contract() -> None:
    print("\n--- 7. Snapshot Feature-Kontrakt ---")
    reg = ZoneRegistry(max_zones=3)
    reg.rebuild([std_profile("London"), std_profile("New York")])
    ctx = reg.snapshot(65010.0)

    for key in ("zones", "zone_count", "n_unrepaired_single_prints",
                "zone_at", "zone_below", "zone_above"):
        check(f"key '{key}' vorhanden", key in ctx)
    check("zones gedeckelt auf 3", len(ctx["zones"]) <= 3, f"got {len(ctx['zones'])}")
    check("zone_count = alle Zonen", ctx["zone_count"] >= len(ctx["zones"]))
    check("unrepaired single prints >= 1", ctx["n_unrepaired_single_prints"] >= 1)

    ctx_no_price = reg.snapshot(None)
    check("ohne Preis: zone_at None", ctx_no_price["zone_at"] is None)


def test_deterministic() -> None:
    print("\n--- 8. Deterministisch ---")
    profiles = [std_profile("London"), std_profile("New York")]
    z1 = [z.to_dict() for z in build_zones(profiles)]
    z2 = [z.to_dict() for z in build_zones(profiles)]
    check("build_zones stabil", z1 == z2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("  business_zones — Step 6 Test Suite")
    print("=" * 60)

    test_zone_derivation()
    test_merge_recurrence()
    test_distinct_pocs_not_merged()
    test_nearest_zones()
    test_state_machine()
    test_state_survives_rebuild()
    test_snapshot_contract()
    test_deterministic()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"  RESULT: {PASS} passed, {FAIL} failed  (total {total})")
    print(f"{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
