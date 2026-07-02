"""detect_delta_divergence — Sprint B test suite.

Covers: bearish/bullish divergence, no-divergence cases, spot confirmation,
strength normalisation, both-sides tie-break, input validation,
DivergenceDetector swing series accessors.

Run: python tests/test_delta_divergence.py
"""

import sys
sys.path.insert(0, ".")

from core.pattern_engine import detect_delta_divergence
from core.divergence import DivergenceDetector

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
# 1. Bearish divergence: price HH, CVD lower high
# ---------------------------------------------------------------------------

def test_bearish_divergence() -> None:
    print("\n--- 1. Bearish: Preis HH, CVD LH ---")
    result = detect_delta_divergence(
        price_highs=[65000.0, 65500.0],
        cvd_perps_highs=[120.0, 80.0],
    )
    check("result not None", result is not None)
    if result:
        check("type = bearish_divergence", result["divergence_type"] == "bearish_divergence")
        check("price = last high", result["price"] == 65500.0)
        check("prev_price = prior high", result["prev_price"] == 65000.0)
        check("strength > 0", result["strength"] > 0)
        check("spot_confirms is None (kein Spot)", result["spot_confirms"] is None)


# ---------------------------------------------------------------------------
# 2. Bullish divergence: price LL, CVD higher low
# ---------------------------------------------------------------------------

def test_bullish_divergence() -> None:
    print("\n--- 2. Bullish: Preis LL, CVD HL ---")
    result = detect_delta_divergence(
        price_lows=[64000.0, 63500.0],
        cvd_perps_lows=[-150.0, -90.0],
    )
    check("result not None", result is not None)
    if result:
        check("type = bullish_divergence", result["divergence_type"] == "bullish_divergence")
        check("price = last low", result["price"] == 63500.0)
        check("strength > 0", result["strength"] > 0)


# ---------------------------------------------------------------------------
# 3. No divergence: price HH + CVD HH (trend confirmed)
# ---------------------------------------------------------------------------

def test_trend_confirmed_no_divergence() -> None:
    print("\n--- 3. Kein Signal: Preis HH + CVD HH ---")
    result = detect_delta_divergence(
        price_highs=[65000.0, 65500.0],
        cvd_perps_highs=[80.0, 120.0],
    )
    check("HH+HH -> None", result is None)

    result = detect_delta_divergence(
        price_lows=[64000.0, 63500.0],
        cvd_perps_lows=[-90.0, -150.0],
    )
    check("LL+LL -> None", result is None)


# ---------------------------------------------------------------------------
# 4. No divergence: price makes no new extreme
# ---------------------------------------------------------------------------

def test_no_new_price_extreme() -> None:
    print("\n--- 4. Kein Signal: Preis ohne neues Extrem ---")
    result = detect_delta_divergence(
        price_highs=[65500.0, 65000.0],   # lower high
        cvd_perps_highs=[120.0, 80.0],
    )
    check("Preis LH -> None", result is None)

    result = detect_delta_divergence(
        price_lows=[63500.0, 64000.0],    # higher low
        cvd_perps_lows=[-150.0, -90.0],
    )
    check("Preis HL -> None", result is None)


# ---------------------------------------------------------------------------
# 5. Equal CVD counts as "no new high" (divergence, strength 0)
# ---------------------------------------------------------------------------

def test_equal_cvd_is_divergence() -> None:
    print("\n--- 5. CVD exakt gleich = kein neues High -> Divergenz ---")
    result = detect_delta_divergence(
        price_highs=[65000.0, 65500.0],
        cvd_perps_highs=[100.0, 100.0],
    )
    check("result not None", result is not None)
    if result:
        check("strength = 0", result["strength"] == 0.0)


# ---------------------------------------------------------------------------
# 6. Spot confirmation
# ---------------------------------------------------------------------------

def test_spot_confirmation() -> None:
    print("\n--- 6. Spot-CVD Bestätigung ---")
    # Spot also turns down -> confirms bearish
    result = detect_delta_divergence(
        price_highs=[65000.0, 65500.0],
        cvd_perps_highs=[120.0, 80.0],
        cvd_spot_highs=[60.0, 40.0],
    )
    check("spot dreht mit -> True", result is not None and result["spot_confirms"] is True)

    # Spot keeps rising -> does NOT confirm
    result = detect_delta_divergence(
        price_highs=[65000.0, 65500.0],
        cvd_perps_highs=[120.0, 80.0],
        cvd_spot_highs=[40.0, 60.0],
    )
    check("spot steigt weiter -> False", result is not None and result["spot_confirms"] is False)

    # Bullish side: spot also makes higher low -> confirms
    result = detect_delta_divergence(
        price_lows=[64000.0, 63500.0],
        cvd_perps_lows=[-150.0, -90.0],
        cvd_spot_lows=[-80.0, -50.0],
    )
    check("bullish spot HL -> True", result is not None and result["spot_confirms"] is True)


# ---------------------------------------------------------------------------
# 7. Strength normalisation stays in [0, 1]
# ---------------------------------------------------------------------------

def test_strength_bounds() -> None:
    print("\n--- 7. Strength in [0, 1] ---")
    cases = [
        ([65000.0, 65500.0], [1000.0, -1000.0]),   # extreme flip
        ([65000.0, 65500.0], [0.0001, 0.0]),        # tiny values
        ([65000.0, 65500.0], [50.0, 49.9]),         # marginal
    ]
    for ph, ch in cases:
        r = detect_delta_divergence(price_highs=ph, cvd_perps_highs=ch)
        check(f"cvd {ch} -> strength in [0,1]",
              r is not None and 0.0 <= r["strength"] <= 1.0,
              f"got {r['strength'] if r else None!r}")


# ---------------------------------------------------------------------------
# 8. Both sides fire -> stronger one wins
# ---------------------------------------------------------------------------

def test_both_sides_tiebreak() -> None:
    print("\n--- 8. Beide Seiten -> stärkere gewinnt ---")
    result = detect_delta_divergence(
        price_highs=[65000.0, 65500.0],
        cvd_perps_highs=[100.0, 90.0],      # weak bearish (10% gap)
        price_lows=[64000.0, 63500.0],
        cvd_perps_lows=[-100.0, -10.0],     # strong bullish (90% gap)
    )
    check("bullish gewinnt", result is not None
          and result["divergence_type"] == "bullish_divergence")


# ---------------------------------------------------------------------------
# 9. Insufficient / missing input -> None (pure function, no crash)
# ---------------------------------------------------------------------------

def test_insufficient_input() -> None:
    print("\n--- 9. Zu wenig Input -> None ---")
    check("keine Argumente", detect_delta_divergence() is None)
    check("nur 1 Swing", detect_delta_divergence(
        price_highs=[65000.0], cvd_perps_highs=[100.0]) is None)
    check("leere Listen", detect_delta_divergence(
        price_highs=[], cvd_perps_highs=[]) is None)
    check("nur Preis ohne CVD", detect_delta_divergence(
        price_highs=[65000.0, 65500.0]) is None)


# ---------------------------------------------------------------------------
# 10. Determinism
# ---------------------------------------------------------------------------

def test_deterministic() -> None:
    print("\n--- 10. Deterministisch ---")
    args = dict(price_highs=[65000.0, 65500.0], cvd_perps_highs=[120.0, 80.0])
    r1 = detect_delta_divergence(**args)
    r2 = detect_delta_divergence(**args)
    check("gleicher Input -> gleicher Output", r1 == r2)


# ---------------------------------------------------------------------------
# 11. DivergenceDetector swing accessors feed the function
# ---------------------------------------------------------------------------

def test_detector_swing_series() -> None:
    print("\n--- 11. DivergenceDetector Swing-Serien ---")
    det = DivergenceDetector(lookback=2)

    # Two price peaks: 65500 (CVD 100) then 66000 (CVD 60) -> bearish setup.
    # lookback=2 requires 2 bars each side of a swing to confirm it.
    prices = [65000, 65200, 65500, 65200, 65000,   # peak 1 @ 65500
              65300, 66000, 65600, 65300]           # peak 2 @ 66000
    cvds   = [10,    50,    100,   70,    40,
              50,    60,    30,    10]
    for i, (p, c) in enumerate(zip(prices, cvds)):
        det.ingest(1_000_000 + i * 60_000, float(p), float(c))

    highs = det.get_swing_highs()
    check("mind. 2 Swing Highs erkannt", len(highs) >= 2, f"got {len(highs)}")
    if len(highs) >= 2:
        result = detect_delta_divergence(
            price_highs=[s["price"] for s in highs],
            cvd_perps_highs=[s["cvd"] for s in highs],
        )
        check("bearish aus Detector-Swings", result is not None
              and result["divergence_type"] == "bearish_divergence",
              f"got {result!r}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 60)
    print("  detect_delta_divergence — Sprint B Test Suite")
    print("=" * 60)

    test_bearish_divergence()
    test_bullish_divergence()
    test_trend_confirmed_no_divergence()
    test_no_new_price_extreme()
    test_equal_cvd_is_divergence()
    test_spot_confirmation()
    test_strength_bounds()
    test_both_sides_tiebreak()
    test_insufficient_input()
    test_deterministic()
    test_detector_swing_series()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"  RESULT: {PASS} passed, {FAIL} failed  (total {total})")
    print(f"{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
