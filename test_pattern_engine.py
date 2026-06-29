"""Sprint P0 -- Pattern Engine + Aggregator Integration Tests.

Tests the complete detection pipeline WITHOUT live data:
  1. PatternEngine unit tests with known mean/std baseline
  2. z-score correctly identifies significant volume
  3. Absorption flag triggers correctly
  4. Engine runs in degraded mode when baseline missing (no crash)
  5. Integration: PatternEngine via aggregator -> PATTERNS channel
  6. Session VAP reset
  7. min_weeks guard: buckets with insufficient weeks are skipped

Usage:
  python test_pattern_engine.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

import pandas as pd

from core.broker import Broker, PATTERNS, AGGREGATED, TRADES
from core.pattern_engine import PatternEngine, BUCKET, DEFAULT_MIN_WEEKS
from core.cvd import CVD


# ------------------------------------------------------------------
# Helper: build a synthetic baseline parquet file
# ------------------------------------------------------------------
def _build_synthetic_baseline(path: Path, extra_rows: list = None) -> None:
    """Create a synthetic baseline with known mean/std per bucket.

    Default buckets:
      Bucket 50000: mean=10.0, std=2.0, weeks=4
      Bucket 50100: mean=5.0,  std=1.0, weeks=4
      Bucket 50200: mean=20.0, std=5.0, weeks=3
    """
    rows = extra_rows or [
        {"price_bucket": 50000, "mean_volume": 10.0, "std_volume": 2.0, "week_count": 4},
        {"price_bucket": 50100, "mean_volume": 5.0,  "std_volume": 1.0, "week_count": 4},
        {"price_bucket": 50200, "mean_volume": 20.0, "std_volume": 5.0, "week_count": 3},
    ]
    df = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, compression="snappy")
    print(f"  Synthetic baseline written to {path}")


def _cleanup_baseline(path: Path) -> None:
    """Remove the synthetic baseline file."""
    if path.exists():
        path.unlink()
        print(f"  Cleaned up {path}")


# ------------------------------------------------------------------
# Part 1: Pattern Engine Unit Tests
# ------------------------------------------------------------------
def test_engine_degraded_mode() -> None:
    """Engine should work (return None, no crash) without baseline."""
    print("=" * 60)
    print("Part 1: Degraded mode (no baseline)")
    print("=" * 60)

    engine = PatternEngine()
    assert not engine.has_baseline, "Engine should have no baseline"

    # Calling evaluate should not crash
    result = engine.evaluate({"price": 50000.0, "size": 1.0, "side": "buy"})
    assert result is None, f"Expected None in degraded mode, got {result}"

    # Session VAP should still accumulate
    vap = engine.get_session_vap()
    assert len(vap) > 0, "Session VAP should not be empty"
    print(f"  Session VAP: {vap}")
    print("[PASS] Degraded mode works without crash")
    print()


def test_engine_with_baseline() -> None:
    """Engine should detect significance with a valid baseline."""
    print("=" * 60)
    print("Part 2: z-score significance detection")
    print("=" * 60)

    baseline_path = Path("data/test_baseline.parquet")
    _build_synthetic_baseline(baseline_path)

    engine = PatternEngine(z_threshold=2.0, min_weeks=2, baseline_path=baseline_path)
    assert engine.has_baseline, "Engine should have loaded the baseline"

    engine.reset_session_vap()

    # Fill bucket 50000 with volume = 14 (mean=10, std=2 => z=2.0)
    for _ in range(14):
        engine.evaluate({"price": 50000.0, "size": 1.0, "side": "buy", "timestamp": 1000})

    bucket = int(50000.0 / BUCKET) * BUCKET
    # After dict conversion, access via _baseline dict
    info = engine._baseline[bucket]
    mean_vol = info["mean"]
    std_vol = info["std"]
    current_vol = engine._session_vap.get(bucket, 0.0)
    z_score = (current_vol - mean_vol) / std_vol
    print(f"  Bucket {bucket}: mean={mean_vol}, std={std_vol}, current={current_vol}, z={z_score:.4f}")
    assert z_score >= 2.0, f"z-score {z_score:.4f} should be >= 2.0"
    print("  [PASS] Threshold boundary works")

    # High volume triggers significance
    result = engine.evaluate(
        {"price": 50000.0, "size": 10.0, "side": "buy", "timestamp": 2000},
        recent_cvd_snapshot={"rolling_delta": 0.5, "rolling_buy_volume": 12.0, "rolling_sell_volume": 11.0},
    )
    assert result is not None, "Should return a pattern hit at high volume"
    assert result["price_bucket"] == 50000
    assert result["z_score"] > 2.0
    print(f"  Pattern hit: bucket={result['price_bucket']}, z={result['z_score']:.2f}, "
          f"vol={result['volume']:.1f}, absorption={result['absorption']}")
    print("  [PASS] High volume triggers significance")

    # Unknown bucket returns None
    result = engine.evaluate({"price": 99999.0, "size": 1.0, "side": "buy"})
    assert result is None, "Unknown bucket should return None"
    print("  [PASS] Unknown bucket returns None")

    # Low volume below threshold returns None
    engine.reset_session_vap()
    engine.evaluate({"price": 50100.0, "size": 5.0, "side": "buy"})  # z=0
    result = engine.evaluate({"price": 50100.0, "size": 0.01, "side": "buy"})  # z=0.01
    assert result is None, f"z-score 0.01 should be below threshold, got {result}"
    print("  [PASS] Low volume below threshold returns None")

    _cleanup_baseline(baseline_path)
    print()


def test_absorption_flag() -> None:
    """Absorption flag should trigger when |delta|/volume is small."""
    print("=" * 60)
    print("Part 3: Absorption detection")
    print("=" * 60)

    baseline_path = Path("data/test_baseline_abs.parquet")
    _build_synthetic_baseline(baseline_path)

    engine = PatternEngine(z_threshold=2.0, absorption_ratio=0.15, min_weeks=2, baseline_path=baseline_path)
    engine.reset_session_vap()

    # Push bucket 50000 to significant volume
    for _ in range(15):
        engine.evaluate({"price": 50000.0, "size": 1.0, "side": "buy"})

    # Small delta/volume ratio -> absorption
    result = engine.evaluate(
        {"price": 50000.0, "size": 1.0, "side": "buy", "timestamp": 3000},
        recent_cvd_snapshot={"rolling_delta": 0.5, "rolling_buy_volume": 10.0, "rolling_sell_volume": 9.5},
    )
    assert result is not None, "Should get a pattern hit"
    assert result["absorption"] is True, f"Expected absorption=True, got {result}"
    print(f"  Absorption detected: bucket={result['price_bucket']}, "
          f"z={result['z_score']:.2f}, absorption={result['absorption']}")
    print("  [PASS] Absorption flag triggers when delta/volume ratio is small")

    # Large delta -> no absorption
    engine.reset_session_vap()
    for _ in range(15):
        engine.evaluate({"price": 50000.0, "size": 1.0, "side": "buy"})

    result = engine.evaluate(
        {"price": 50000.0, "size": 1.0, "side": "buy", "timestamp": 4000},
        recent_cvd_snapshot={"rolling_delta": 15.0, "rolling_buy_volume": 20.0, "rolling_sell_volume": 5.0},
    )
    assert result is not None, "Should get a pattern hit"
    assert result["absorption"] is False, f"Expected absorption=False for large delta, got {result}"
    print(f"  No absorption (large delta): bucket={result['price_bucket']}, "
          f"z={result['z_score']:.2f}, absorption={result['absorption']}")
    print("  [PASS] Large delta correctly suppresses absorption flag")

    # No CVD snapshot -> absorption defaults to False
    engine.reset_session_vap()
    for _ in range(15):
        engine.evaluate({"price": 50000.0, "size": 1.0, "side": "buy"})

    result = engine.evaluate(
        {"price": 50000.0, "size": 1.0, "side": "buy", "timestamp": 5000},
    )
    assert result is not None, "Should get a pattern hit"
    assert result["absorption"] is False, f"Expected absorption=False without CVD snapshot, got {result}"
    print("  [PASS] No CVD snapshot -> absorption defaults to False")

    _cleanup_baseline(baseline_path)
    print()


def test_min_weeks_guard() -> None:
    """Buckets with insufficient weeks should never return a pattern hit."""
    print("=" * 60)
    print("Part 4: min_weeks guard")
    print("=" * 60)

    baseline_path = Path("data/test_baseline_weeks.parquet")
    rows = [
        {"price_bucket": 50000, "mean_volume": 10.0, "std_volume": 2.0, "week_count": 1},
        {"price_bucket": 50100, "mean_volume": 10.0, "std_volume": 2.0, "week_count": 2},
        {"price_bucket": 50200, "mean_volume": 10.0, "std_volume": 2.0, "week_count": 3},
    ]
    _build_synthetic_baseline(baseline_path, extra_rows=rows)

    # min_weeks=2: bucket 50000 (week_count=1) should be ineligible
    engine = PatternEngine(z_threshold=1.0, min_weeks=2, baseline_path=baseline_path)
    engine.reset_session_vap()

    # Push bucket 50000 well past the z-threshold (volume=30, mean=10, std=2 => z=10)
    for _ in range(30):
        engine.evaluate({"price": 50000.0, "size": 1.0, "side": "buy", "timestamp": 1000})
    result = engine.evaluate({"price": 50000.0, "size": 1.0, "side": "buy", "timestamp": 2000})
    assert result is None, (
        f"Bucket with week_count=1 should be ineligible with min_weeks=2, got {result}"
    )
    print("  [PASS] Bucket with week_count=1 correctly skipped (below min_weeks=2)")

    # Bucket 50100 (week_count=2, exactly at min_weeks) should be eligible
    engine.reset_session_vap()
    for _ in range(30):
        engine.evaluate({"price": 50100.0, "size": 1.0, "side": "buy", "timestamp": 1000})
    result = engine.evaluate({"price": 50100.0, "size": 1.0, "side": "buy", "timestamp": 2000})
    assert result is not None, (
        f"Bucket with week_count=2 should be eligible with min_weeks=2, got None"
    )
    print(f"  [PASS] Bucket with week_count=2 correctly eligible, z={result['z_score']:.2f}")

    _cleanup_baseline(baseline_path)
    print()


# ------------------------------------------------------------------
# Part 5: Integration via Broker (PatternEngine -> PATTERNS channel)
# ------------------------------------------------------------------
async def test_broker_integration() -> None:
    """Verify patterns are published on the PATTERNS channel."""
    print("=" * 60)
    print("Part 5: Broker integration (PatternEngine -> PATTERNS)")
    print("=" * 60)

    baseline_path = Path("data/test_baseline_int.parquet")
    rows = [
        {"price_bucket": 50000, "mean_volume": 10.0, "std_volume": 2.0, "week_count": 4},
    ]
    df = pd.DataFrame(rows)
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(baseline_path, index=False, compression="snappy")

    broker = Broker()
    engine = PatternEngine(z_threshold=2.0, absorption_ratio=0.15, min_weeks=2, baseline_path=baseline_path)
    cvd = CVD(window_size=200)

    pattern_q = broker.subscribe(PATTERNS)
    broker.subscribe(TRADES)
    broker.subscribe(AGGREGATED)

    engine.reset_session_vap()
    collected_patterns = []

    for i in range(16):
        trade = {"price": 50000.0, "size": 1.0, "side": "buy", "timestamp": 1000 + i}
        snap = cvd.update(50000.0, 1.0, "buy", timestamp=1000 + i)
        result = engine.evaluate(trade, recent_cvd_snapshot=snap)
        if result is not None:
            collected_patterns.append(result)
            await broker.publish(PATTERNS, result)

    print(f"  Engine produced {len(collected_patterns)} pattern hits from 16 trades")

    received = []
    while True:
        try:
            msg = pattern_q.get_nowait()
            received.append(msg)
        except asyncio.QueueEmpty:
            break

    print(f"  Broker received {len(received)} pattern messages")
    assert len(collected_patterns) > 0, "Should have detected at least one pattern"
    assert len(received) > 0, "Should have received at least one pattern via broker"

    msg = received[0]
    assert "price_bucket" in msg
    assert "z_score" in msg
    assert "volume" in msg
    assert "absorption" in msg
    assert "timestamp" in msg
    print(f"  Pattern schema valid: bucket={msg['price_bucket']}, "
          f"z={msg['z_score']:.2f}, abs={msg['absorption']}")
    print("[PASS] Broker integration works correctly")

    _cleanup_baseline(baseline_path)
    print()


# ------------------------------------------------------------------
# Part 6: Session VAP reset
# ------------------------------------------------------------------
def test_session_vap_reset() -> None:
    """Verify session VAP can be reset independently."""
    print("=" * 60)
    print("Part 6: Session VAP reset")
    print("=" * 60)

    engine = PatternEngine()
    engine.reset_session_vap()

    assert len(engine.get_session_vap()) == 0, "Session VAP should be empty after reset"

    engine.evaluate({"price": 50000.0, "size": 1.0, "side": "buy"})
    assert len(engine.get_session_vap()) > 0, "Session VAP should have data after trades"

    engine.reset_session_vap()
    assert len(engine.get_session_vap()) == 0, "Session VAP should be empty after second reset"
    print("  [PASS] Reset clears session VAP")
    print()


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
async def run_all() -> int:
    passed = 0
    failed = 0

    sync_tests = [
        ("Degraded mode", lambda: test_engine_degraded_mode()),
        ("z-score significance", lambda: test_engine_with_baseline()),
        ("Absorption detection", lambda: test_absorption_flag()),
        ("min_weeks guard", lambda: test_min_weeks_guard()),
        ("Session VAP reset", lambda: test_session_vap_reset()),
    ]

    for name, fn in sync_tests:
        try:
            fn()
            passed += 1
            print(f"[PASS] {name}")
        except AssertionError as e:
            print(f"[FAIL] {name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
        except Exception as e:
            print(f"[FAIL] {name} Exception: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
        print()

    async_tests = [
        ("Broker integration", lambda: test_broker_integration()),
    ]

    for name, fn in async_tests:
        try:
            await fn()
            passed += 1
            print(f"[PASS] {name}")
        except AssertionError as e:
            print(f"[FAIL] {name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
        except Exception as e:
            print(f"[FAIL] {name} Exception: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
        print()

    print("=" * 60)
    print(f"RESULT: {passed} passed, {failed} failed")
    print("=" * 60)
    return 0 if failed == 0 else 1


def main() -> int:
    return asyncio.run(run_all())


if __name__ == "__main__":
    sys.exit(main())
