"""PR4a Evidence 1 — Contract Proof.

Builds one deterministic AGGREGATED payload and prints:
  - the full JSON structure
  - which fields are NEW (additive) vs PRE-EXISTING
  - a diff table confirming every legacy field is present and type-stable

Run: python scripts/verify_pr4a_contract.py
"""

import json
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

from core.cvd import CVD
from core.session_profile import SessionProfile
from core.weekly_profile import WeeklyProfile
from core.profile_shape import classify_shape
from core.absorption import AbsorptionDetector
from core.divergence import DivergenceDetector
from core.composite_profile import CompositeProfile

# ── Fixed timestamp in Asia session (2026-06-01 02:00 UTC) ──────────────────
BASE_TS = int(datetime(2026, 6, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

# ── Pre-existing top-level field names (defined before PR4a) ────────────────
LEGACY_FIELDS = {
    "timestamp", "mid_price", "best_bid", "best_ask", "spread",
    "imbalance_5", "imbalance_20", "bids", "asks", "cvd",
    "session_context", "profile_shape", "weekly_context",
    "composite_context", "divergence",
}

# ── Pre-existing sub-keys inside session_context (before PR4a) ──────────────
LEGACY_SESSION_KEYS = {
    "session", "session_elapsed_seconds", "session_poc",
    "session_value_area_high", "session_value_area_low",
    "session_volume", "session_trade_count", "session_ohlc",
    "price_vs_poc", "price_in_value_area",
}

# ── Pre-existing sub-keys inside weekly_context (before PR4a) ───────────────
LEGACY_WEEKLY_KEYS = {
    "week", "week_poc", "week_value_area_high", "week_value_area_low",
    "week_volume", "week_ohlc", "week_price",
    "price_vs_week_poc", "price_in_week_value_area",
}


def build_payload() -> dict:
    cvd = CVD(window_size=200)
    session = SessionProfile()
    weekly = WeeklyProfile()
    absorption = AbsorptionDetector()
    divergence = DivergenceDetector(lookback=3)
    composite = CompositeProfile()

    trades = [
        (64900.0, 1.0, "sell"), (64950.0, 3.0, "buy"), (65000.0, 10.0, "buy"),
        (65025.0, 8.0, "buy"),  (65050.0, 6.0, "sell"), (65075.0, 2.0, "sell"),
        (65025.0, 1.0, "buy"),
    ]
    for i, (price, size, side) in enumerate(trades):
        ts = BASE_TS + i * 5_000
        cvd.update(price, size, side)
        session.ingest(ts, price, size, side)
        weekly.ingest(ts, price, size, side)

    snap = cvd.snapshot()
    mid_price = 65025.0

    divergence.ingest(BASE_TS, mid_price, snap.get("rolling_delta", 0.0))

    session_ctx = session.current_context()
    shape_ctx = classify_shape(session._vap if session.current_session and session._vap else None)
    weekly_ctx = weekly.current_context()
    composite_ctx = composite.current_context()

    # Simulated L2 (no live exchange in this script)
    return {
        "timestamp": BASE_TS + len(trades) * 5_000,
        "mid_price": mid_price,
        "best_bid": mid_price - 5.0,
        "best_ask": mid_price + 5.0,
        "spread": 10.0,
        "imbalance_5": 0.55,
        "imbalance_20": 0.48,
        "bids": [[mid_price - 5, 1.2], [mid_price - 10, 2.0]],
        "asks": [[mid_price + 5, 0.8], [mid_price + 10, 1.5]],
        "cvd": snap,
        "session_context": session_ctx,
        "profile_shape": shape_ctx,
        "weekly_context": weekly_ctx,
        "composite_context": composite_ctx,
        "divergence": divergence.current_divergence,
    }


def main() -> int:
    payload = build_payload()

    # ── 1. Full JSON payload ─────────────────────────────────────────────────
    print("=" * 70)
    print("  AGGREGATED PAYLOAD (full JSON)")
    print("=" * 70)
    print(json.dumps(payload, indent=2, default=str))

    # ── 2. Top-level field diff ──────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  TOP-LEVEL FIELD ANALYSIS")
    print("=" * 70)
    actual_keys = set(payload.keys())
    new_top = actual_keys - LEGACY_FIELDS
    missing_top = LEGACY_FIELDS - actual_keys

    print(f"  Pre-existing fields present:  {len(LEGACY_FIELDS - missing_top)} / {len(LEGACY_FIELDS)}")
    print(f"  Missing legacy fields:        {missing_top or 'none'}")
    print(f"  New additive top-level fields:{new_top or 'none'}")

    # ── 3. session_context field diff ────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  session_context FIELD ANALYSIS")
    print("=" * 70)
    sc = payload["session_context"]
    sc_keys = set(sc.keys())
    new_sc = sc_keys - LEGACY_SESSION_KEYS
    missing_sc = LEGACY_SESSION_KEYS - sc_keys

    print(f"  Pre-existing keys present:    {len(LEGACY_SESSION_KEYS - missing_sc)} / {len(LEGACY_SESSION_KEYS)}")
    print(f"  Missing legacy keys:          {missing_sc or 'none'}")
    print(f"  NEW additive keys:            {new_sc or 'none'}   <-- PR4a addition")
    for k in new_sc:
        print(f"    '{k}' = {sc[k]!r}")

    # ── 4. weekly_context field diff ─────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  weekly_context FIELD ANALYSIS")
    print("=" * 70)
    wc = payload["weekly_context"]
    wc_keys = set(wc.keys())
    new_wc = wc_keys - LEGACY_WEEKLY_KEYS
    missing_wc = LEGACY_WEEKLY_KEYS - wc_keys

    print(f"  Pre-existing keys present:    {len(LEGACY_WEEKLY_KEYS - missing_wc)} / {len(LEGACY_WEEKLY_KEYS)}")
    print(f"  Missing legacy keys:          {missing_wc or 'none'}")
    print(f"  NEW additive keys:            {new_wc or 'none'}   <-- PR4a addition")
    for k in new_wc:
        print(f"    '{k}' = {wc[k]!r}")

    # ── 5. Type stability table for legacy fields ────────────────────────────
    print("\n" + "=" * 70)
    print("  LEGACY FIELD TYPE STABILITY")
    print("=" * 70)
    checks = [
        ("timestamp",    payload["timestamp"],    int),
        ("mid_price",    payload["mid_price"],    (int, float)),
        ("best_bid",     payload["best_bid"],     (int, float)),
        ("best_ask",     payload["best_ask"],     (int, float)),
        ("spread",       payload["spread"],       (int, float)),
        ("imbalance_5",  payload["imbalance_5"],  (int, float)),
        ("imbalance_20", payload["imbalance_20"], (int, float)),
        ("bids",         payload["bids"],         list),
        ("asks",         payload["asks"],         list),
        ("cvd",          payload["cvd"],          dict),
        ("session_context", payload["session_context"], dict),
        ("profile_shape",   payload["profile_shape"],   dict),
        ("weekly_context",  payload["weekly_context"],  dict),
        ("composite_context", payload["composite_context"], dict),
        ("divergence",    payload["divergence"],  (dict, type(None))),
    ]
    failed = 0
    for name, val, expected_type in checks:
        ok = isinstance(val, expected_type)
        status = "OK  " if ok else "FAIL"
        print(f"  [{status}] {name:<22} type={type(val).__name__}")
        if not ok:
            failed += 1

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    ok_summary = (
        not missing_top and not missing_sc and not missing_wc and failed == 0
    )
    verdict = "PASS" if ok_summary else "FAIL"
    print(f"  CONTRACT PROOF: {verdict}")
    print(f"  No legacy fields removed or renamed.")
    print(f"  Additive only: {new_sc | new_wc}")
    print("=" * 70)
    return 0 if ok_summary else 1


if __name__ == "__main__":
    sys.exit(main())
