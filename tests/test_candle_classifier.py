"""
Tests for core/candle_classifier.py
=====================================
Every threshold is pinned to the exact Pine Script line it comes from.
File under test: trading/nci_system_m1_candle_classifier.pine

Pine Script reference (v5) — condensed logic:
----------------------------------------------
  body      = abs(close - open)
  totalLen  = high - low
  bodyRatio = totalLen > 0 ? body / totalLen : 0.0

  avgMaru5  = array.size(mBuf) > 0 ? array.avg(mBuf) : na   ← computed BEFORE classify

  isMarubozu  = totalLen > 0 and bodyRatio >= 0.70
  inAbsZone   = close >= high - 0.10*totalLen  or  close <= low + 0.10*totalLen
  isShockMove = not isMarubozu and totalLen > 0 and bodyRatio >= 0.50 and inAbsZone
  isDoji      = not isMarubozu and not isShockMove
                  and not na(avgMaru5) and totalLen < 0.30 * avgMaru5
  isPinbar    = not isMarubozu and not isShockMove and not isDoji
                  and bodyRatio < 0.50 and not na(avgMaru5) and totalLen >= 0.30*avgMaru5
  isNormal    = everything else  (incl. when avgMaru5 is na)

  Buffer update (AFTER classify): push totalLen if isMarubozu or isShockMove; keep last 5.
"""

import sys

import pytest

sys.path.insert(0, ".")

from core.candle_classifier import (
    CandleClassifier,
    CandleResult,
    classify_series,
    MARUBOZU,
    SHOCK_MOVE,
    DOJI,
    PINBAR,
    INSIDE_BAR,
    NORMAL,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clf() -> CandleClassifier:
    """Fresh classifier with empty benchmark buffer."""
    return CandleClassifier()


def _feed_strong(clf: CandleClassifier, total_len: float, n: int = 1) -> None:
    """Feed *n* Marubozu candles of the given total length to prime the benchmark."""
    for _ in range(n):
        # Fully bodied bull candle: open=low, close=high → body/total = 1.0
        clf.classify(open_=0.0, high=total_len, low=0.0, close=total_len)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Marubozu — Pine: bodyRatio >= 0.70
# ─────────────────────────────────────────────────────────────────────────────

class TestMarubozu:

    def test_exact_boundary_070_is_marubozu(self):
        # body=7, total=10, ratio=0.70 → exactly at threshold → Marubozu
        r = _clf().classify(open_=100, high=110, low=100, close=107)
        assert r.candle_type == MARUBOZU

    def test_above_boundary_is_marubozu(self):
        # body=9, total=10, ratio=0.90 → Marubozu
        r = _clf().classify(open_=100, high=110, low=100, close=109)
        assert r.candle_type == MARUBOZU

    def test_just_below_070_is_not_marubozu(self):
        # body=6.99, total=10, ratio=0.699 → below threshold
        r = _clf().classify(open_=100, high=110, low=100, close=106.99)
        assert r.candle_type != MARUBOZU

    def test_bull_direction(self):
        # close > open → bull
        r = _clf().classify(open_=100, high=110, low=100, close=107)
        assert r.candle_type == MARUBOZU
        assert r.direction == "bull"

    def test_bear_direction(self):
        # close < open → bear
        r = _clf().classify(open_=107, high=110, low=100, close=100)
        assert r.candle_type == MARUBOZU
        assert r.direction == "bear"

    def test_is_strong_true(self):
        r = _clf().classify(open_=100, high=110, low=100, close=107)
        assert r.is_strong is True

    def test_feeds_benchmark(self):
        # After a Marubozu, avg_maru5 must no longer be None
        clf = _clf()
        assert clf.avg_maru5 is None
        clf.classify(open_=100, high=110, low=100, close=107)
        assert clf.avg_maru5 is not None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Shock Move — Pine: not isMarubozu and bodyRatio >= 0.50 and inAbsZone
#    inAbsZone = close >= high - 0.10*totalLen  OR  close <= low + 0.10*totalLen
# ─────────────────────────────────────────────────────────────────────────────

class TestShockMove:

    def test_bearish_sm_body_050_close_at_bottom_boundary(self):
        # o=106, h=110, l=100, c=101
        # total=10, body=5, ratio=0.50
        # inAbsZone: c=101 <= low+1=101 → True  (boundary inclusive)
        # Not Marubozu (0.50 < 0.70) → Shock Move
        r = _clf().classify(open_=106, high=110, low=100, close=101)
        assert r.candle_type == SHOCK_MOVE
        assert r.direction == "bear"

    def test_bullish_sm_body_050_close_at_top_boundary(self):
        # o=104, h=110, l=100, c=109
        # total=10, body=5, ratio=0.50
        # inAbsZone: c=109 >= high-1=109 → True  (boundary inclusive)
        # Not Marubozu (0.50 < 0.70) → Shock Move
        r = _clf().classify(open_=104, high=110, low=100, close=109)
        assert r.candle_type == SHOCK_MOVE
        assert r.direction == "bull"

    def test_body_060_in_abs_zone_is_sm(self):
        # o=103, h=110, l=100, c=101
        # total=10, body=2, ratio=0.20 — wait, that's body<0.50, not SM
        # Redo: bearish: o=109, h=110, l=100, c=103
        # total=10, body=6, ratio=0.60, close=103 <= 100+1? No. 103 >= 110-1=109? No → not in zone
        # Need close near bottom: o=109, h=110, l=100, c=100.5
        # total=10, body=8.5, ratio=0.85 → Marubozu (>0.70)  — too large
        # o=106.5, h=110, l=100, c=100.5. total=10, body=6, ratio=0.60.
        # close=100.5 <= 100+1=101 → True. Not Marubozu → Shock Move!
        r = _clf().classify(open_=106.5, high=110, low=100, close=100.5)
        assert r.candle_type == SHOCK_MOVE
        assert r.body_ratio == pytest.approx(0.60)

    def test_body_069_in_abs_zone_is_sm_not_marubozu(self):
        # bodyRatio=0.69 is just below Marubozu threshold (0.70)
        # total=10, body=6.9, ratio=0.69.  close near bottom.
        # o=107.4, h=110.4, l=100, c=100.5  → body=6.9, total=10.4... messy
        # Simpler: total=100, body=69. o=169, h=200, l=100, c=100. body=69, total=100.
        # inAbsZone: c=100 <= 100+10=110 → True. ratio=0.69 < 0.70 → NOT Marubozu → SM!
        r = _clf().classify(open_=169, high=200, low=100, close=100)
        assert r.candle_type == SHOCK_MOVE
        assert r.body_ratio == pytest.approx(0.69)

    def test_body_050_outside_abs_zone_is_not_sm(self):
        # o=103, h=110, l=100, c=108
        # total=10, body=5, ratio=0.50
        # close=108: 108 >= 110-1=109? No.  108 <= 100+1=101? No. → NOT in zone → not SM
        # No benchmark → Normal
        r = _clf().classify(open_=103, high=110, low=100, close=108)
        assert r.candle_type == NORMAL

    def test_body_between_050_070_outside_zone_is_normal(self):
        # The "gap region": 0.50 <= bodyRatio < 0.70, close NOT in abs zone
        # → Not Marubozu, Not SM, falls through to Normal (before benchmark)
        # o=104, h=110, l=100, c=106. total=10, body=2, ratio=0.20 — too low
        # o=100, h=110, l=100, c=106. total=10, body=6, ratio=0.60.
        # close=106: 106 >= 109? No. 106 <= 101? No. Not in zone → Normal
        r = _clf().classify(open_=100, high=110, low=100, close=106)
        assert r.candle_type == NORMAL

    def test_sm_feeds_benchmark(self):
        # Shock Move should also feed the avgMaru5 buffer
        clf = _clf()
        clf.classify(open_=106, high=110, low=100, close=101)  # SM, total=10
        assert clf.avg_maru5 == pytest.approx(10.0)

    def test_sm_is_strong(self):
        r = _clf().classify(open_=106, high=110, low=100, close=101)
        assert r.is_strong is True


# ─────────────────────────────────────────────────────────────────────────────
# 3. Doji — Pine: totalLen < 0.30 * avgMaru5  (requires benchmark)
# ─────────────────────────────────────────────────────────────────────────────

class TestDoji:

    def test_doji_requires_benchmark(self):
        # Without any prior strong candle, even a zero-size bar → Normal
        r = _clf().classify(open_=100, high=100.5, low=100, close=100.2)
        assert r.candle_type == NORMAL

    def test_tiny_bar_after_benchmark_is_doji(self):
        # avgMaru5=10 after one Marubozu(total=10).
        # Doji threshold: total < 0.30*10 = 3.0
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        # total=2.0 → 2.0 < 3.0 → Doji
        r = clf.classify(open_=100, high=102, low=100, close=100.5)
        assert r.candle_type == DOJI

    def test_doji_just_below_threshold(self):
        # threshold = 0.30 * avgMaru5. total=2.99 < 3.0 → Doji
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        r = clf.classify(open_=100, high=102.99, low=100, close=100.1)
        assert r.candle_type == DOJI

    def test_doji_exact_boundary_is_not_doji(self):
        # Pine: strict less-than. total = 0.30 * avgMaru5 exactly → NOT Doji
        # avgMaru5=10, threshold=3.0. total=3.0 → NOT Doji
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        # total=3.0: needs body < 0.50*3=1.5 (not SM/Maru), not in abs zone
        # o=100, h=103, l=100, c=100.5. total=3, body=0.5, ratio=0.167
        # inAbsZone: 100.5 >= 103-0.3=102.7? No. 100.5 <= 100+0.3=100.3? No. Not SM.
        # totalLen=3 NOT < 3.0 → NOT Doji → Pinbar (body_ratio=0.167<0.50, total>=3)
        r = clf.classify(open_=100, high=103, low=100, close=100.5)
        assert r.candle_type != DOJI

    def test_doji_is_not_strong(self):
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        benchmark_before = clf.avg_maru5
        r = clf.classify(open_=100, high=102, low=100, close=100.5)
        assert r.candle_type == DOJI
        assert r.is_strong is False
        # benchmark must NOT be updated by Doji
        assert clf.avg_maru5 == benchmark_before

    def test_is_indecision_true(self):
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        r = clf.classify(open_=100, high=102, low=100, close=100.5)
        assert r.is_indecision is True

    def test_zero_length_bar_is_doji_after_benchmark(self):
        # totalLen=0 < 0.30*avgMaru5 once benchmark exists
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        r = clf.classify(open_=100, high=100, low=100, close=100)
        assert r.candle_type == DOJI

    def test_zero_length_bar_is_normal_before_benchmark(self):
        r = _clf().classify(open_=100, high=100, low=100, close=100)
        assert r.candle_type == NORMAL


# ─────────────────────────────────────────────────────────────────────────────
# 4. Pinbar — Pine: bodyRatio < 0.50 and totalLen >= 0.30 * avgMaru5
# ─────────────────────────────────────────────────────────────────────────────

class TestPinbar:

    def test_pinbar_requires_benchmark(self):
        # Without benchmark, even a classically wicky bar → Normal
        # o=100, h=110, l=100, c=101. total=10, body=1, ratio=0.10
        r = _clf().classify(open_=100, high=110, low=100, close=101)
        assert r.candle_type == NORMAL

    def test_pinbar_basic(self):
        # avgMaru5=10. Doji threshold=3.0.
        # Pinbar: total>=3, body_ratio<0.50
        # o=100, h=108, l=100, c=101. total=8, body=1, ratio=0.125.
        # close=101: in_abs_zone? 101 >= 108-0.8=107.2? No. 101 <= 100+0.8=100.8? No.
        # Not SM. NOT Doji (8 >= 3). Pinbar (0.125 < 0.50, 8>=3). ✓
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        r = clf.classify(open_=100, high=108, low=100, close=101)
        assert r.candle_type == PINBAR

    def test_pinbar_at_exact_body_ratio_boundary(self):
        # Pine: bodyRatio < 0.50 (strict). At exactly 0.50 → NOT Pinbar
        # total=10, body=5, ratio=0.50. Close not in abs zone.
        # o=100, h=110, l=100, c=105. total=10, body=5, ratio=0.50.
        # close=105: 105>=109? No. 105<=101? No. Not SM (body=0.50 but not in zone).
        # Not Doji (10 not < 3). Pinbar: body_ratio=0.50 < 0.50? → False → NOT Pinbar → Normal
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        r = clf.classify(open_=100, high=110, low=100, close=105)
        assert r.candle_type == NORMAL

    def test_pinbar_just_below_body_ratio_050(self):
        # body=4.99, total=10, ratio=0.499 < 0.50 → Pinbar
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        # bull pinbar: long lower wick. o=100, h=110, l=100, c=104.99. body=4.99
        # close=104.99: in_abs_zone? 104.99>=109? No. 104.99<=101? No. Not SM.
        r = clf.classify(open_=100, high=110, low=100, close=104.99)
        assert r.candle_type == PINBAR
        assert r.body_ratio < 0.50

    def test_pinbar_not_strong(self):
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        benchmark_before = clf.avg_maru5
        r = clf.classify(open_=100, high=108, low=100, close=101)
        assert r.candle_type == PINBAR
        assert r.is_strong is False
        assert clf.avg_maru5 == benchmark_before

    def test_pinbar_is_indecision(self):
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        r = clf.classify(open_=100, high=108, low=100, close=101)
        assert r.is_indecision is True


# ─────────────────────────────────────────────────────────────────────────────
# 5. Normal — catch-all
# ─────────────────────────────────────────────────────────────────────────────

class TestNormal:

    def test_body_between_050_070_not_in_abs_zone(self):
        # bodyRatio=0.60 → not Maru, not SM (outside zone) → Normal
        r = _clf().classify(open_=100, high=110, low=100, close=106)
        assert r.candle_type == NORMAL

    def test_normal_before_any_benchmark(self):
        # Wicky candle but no benchmark → Normal
        r = _clf().classify(open_=100, high=110, low=90, close=101)
        assert r.candle_type == NORMAL

    def test_normal_does_not_feed_benchmark(self):
        clf = _clf()
        assert clf.avg_maru5 is None
        clf.classify(open_=100, high=110, low=100, close=106)  # Normal
        assert clf.avg_maru5 is None

    def test_normal_is_not_strong(self):
        r = _clf().classify(open_=100, high=110, low=100, close=106)
        assert r.is_strong is False

    def test_normal_is_not_indecision(self):
        r = _clf().classify(open_=100, high=110, low=100, close=106)
        assert r.is_indecision is False


# ─────────────────────────────────────────────────────────────────────────────
# 6. Benchmark (avgMaru5) rolling window — Pine: keep last 5
# ─────────────────────────────────────────────────────────────────────────────

class TestBenchmark:

    def test_empty_benchmark_is_none(self):
        assert _clf().avg_maru5 is None

    def test_first_strong_sets_benchmark(self):
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        assert clf.avg_maru5 == pytest.approx(10.0)

    def test_average_of_two_strong(self):
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        _feed_strong(clf, total_len=20.0)
        assert clf.avg_maru5 == pytest.approx(15.0)

    def test_window_rolls_at_6th_candle(self):
        # First 5 all total=10 → avgMaru5=10
        # 6th strong candle: total=20 → buffer=[10,10,10,10,20] → avg=12
        clf = _clf()
        _feed_strong(clf, total_len=10.0, n=5)
        assert clf.avg_maru5 == pytest.approx(10.0)
        _feed_strong(clf, total_len=20.0)
        assert clf.avg_maru5 == pytest.approx(12.0)

    def test_window_caps_at_5_entries(self):
        # 10 strong candles: first 5 at total=10, next 5 at total=20.
        # After all 10, buffer holds last 5 (all 20) → avg=20
        clf = _clf()
        _feed_strong(clf, total_len=10.0, n=5)
        _feed_strong(clf, total_len=20.0, n=5)
        assert clf.avg_maru5 == pytest.approx(20.0)

    def test_benchmark_uses_pre_classification_value(self):
        # The avgMaru5 in the result must be the benchmark BEFORE the current bar
        # is added — not after.  First bar: avgMaru5 should be None at classify time.
        clf = _clf()
        r = clf.classify(open_=100, high=110, low=100, close=107)  # Marubozu
        assert r.avg_maru5 is None           # benchmark was None before this bar
        assert clf.avg_maru5 is not None     # now updated after

    def test_reset_clears_buffer(self):
        clf = _clf()
        _feed_strong(clf, total_len=10.0, n=3)
        clf.reset()
        assert clf.avg_maru5 is None

    def test_shock_move_feeds_benchmark_same_as_marubozu(self):
        # SM (not Marubozu) must also contribute to buffer
        clf = _clf()
        clf.classify(open_=106, high=110, low=100, close=101)  # SM, total=10
        assert clf.avg_maru5 == pytest.approx(10.0)

    def test_doji_does_not_change_benchmark(self):
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        before = clf.avg_maru5
        # Doji: total=1 < 3
        clf.classify(open_=100, high=101, low=100, close=100.3)
        assert clf.avg_maru5 == before

    def test_normal_does_not_change_benchmark(self):
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        before = clf.avg_maru5
        clf.classify(open_=100, high=110, low=100, close=106)  # Normal
        assert clf.avg_maru5 == before


# ─────────────────────────────────────────────────────────────────────────────
# 7. Priority ordering — first match wins
# ─────────────────────────────────────────────────────────────────────────────

class TestPriorityOrder:

    def test_marubozu_beats_shock_move_criteria(self):
        # A fully bodied candle with close at extreme is Marubozu, not SM
        # o=100, h=110, l=100, c=110 (close=high). body=10, total=10, ratio=1.0
        # inAbsZone=True (close=high). But Marubozu fires first.
        r = _clf().classify(open_=100, high=110, low=100, close=110)
        assert r.candle_type == MARUBOZU

    def test_sm_fires_before_doji_doji_not_applicable(self):
        # If a bar qualifies as SM, Doji is irrelevant regardless of total size.
        # Construct: total < 0.30*avgMaru5 (tiny) BUT body/total >= 0.50 AND in abs zone.
        # avgMaru5=100. Doji threshold: total < 30.
        # tiny SM: total=5 (< 30 → would be Doji), body=3, ratio=0.60, close at top.
        # o=1, h=5, l=0, c=4.7. total=5, body=3.7, ratio=0.74 ≥ 0.70 → Marubozu. Hmm.
        # Try o=2, h=5, l=0, c=4.7. total=5, body=2.7, ratio=0.54.
        # inAbsZone: c=4.7 >= 5-0.5=4.5 → True. Not Marubozu. SM fires before Doji check.
        clf = _clf()
        _feed_strong(clf, total_len=100.0)  # avgMaru5=100, doji threshold=30
        r = clf.classify(open_=2, high=5, low=0, close=4.7)
        # total=5 which is < 30 (doji territory) BUT SM fires first
        assert r.candle_type == SHOCK_MOVE

    def test_full_priority_sequence_in_series(self):
        # Feed a sequence and confirm expected types in order
        clf = _clf()
        r0 = clf.classify(open_=100, high=110, low=100, close=107)   # Marubozu, no benchmark yet
        r1 = clf.classify(open_=100, high=110, low=100, close=107)   # Marubozu, benchmark=10
        r2 = clf.classify(open_=100, high=101, low=100, close=100.5) # total=1 < 3 → Doji
        r3 = clf.classify(open_=100, high=108, low=100, close=101)   # Pinbar
        r4 = clf.classify(open_=100, high=110, low=100, close=106)   # Normal
        assert r0.candle_type == MARUBOZU
        assert r1.candle_type == MARUBOZU
        assert r2.candle_type == DOJI
        assert r3.candle_type == PINBAR
        assert r4.candle_type == NORMAL


# ─────────────────────────────────────────────────────────────────────────────
# 8. Direction — meaningful for all, carries "none" for flat body
# ─────────────────────────────────────────────────────────────────────────────

class TestDirection:

    def test_bull(self):
        r = _clf().classify(open_=100, high=110, low=100, close=107)
        assert r.direction == "bull"

    def test_bear(self):
        r = _clf().classify(open_=107, high=110, low=100, close=100)
        assert r.direction == "bear"

    def test_flat_body_none(self):
        r = _clf().classify(open_=103, high=110, low=100, close=103)
        assert r.direction == "none"


# ─────────────────────────────────────────────────────────────────────────────
# 9. CandleResult fields
# ─────────────────────────────────────────────────────────────────────────────

class TestCandleResult:

    def test_body_ratio_rounded_to_4dp(self):
        # body=7, total=9. ratio=0.7777... → rounds to 0.7778
        r = _clf().classify(open_=100, high=109, low=100, close=107)
        assert r.body_ratio == pytest.approx(0.7778, abs=1e-4)

    def test_total_len(self):
        r = _clf().classify(open_=100, high=115, low=95, close=107)
        assert r.total_len == pytest.approx(20.0)

    def test_avg_maru5_is_none_before_any_strong(self):
        r = _clf().classify(open_=100, high=110, low=100, close=106)
        assert r.avg_maru5 is None

    def test_avg_maru5_reflects_pre_bar_benchmark(self):
        clf = _clf()
        _feed_strong(clf, total_len=10.0, n=2)  # avgMaru5=10 now
        r = clf.classify(open_=100, high=110, low=100, close=106)  # Normal
        assert r.avg_maru5 == pytest.approx(10.0)


# ─────────────────────────────────────────────────────────────────────────────
# 10. classify_series — stateless convenience wrapper
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifySeries:

    def test_returns_same_count(self):
        rows = [(100, 110, 100, 107)] * 5
        results = classify_series(rows)
        assert len(results) == 5

    def test_matches_sequential_classify(self):
        rows = [
            (100, 110, 100, 107),   # Marubozu
            (100, 110, 100, 107),   # Marubozu
            (100, 101, 100, 100.5), # Doji (total=1 < 3=0.3*10)
            (100, 108, 100, 101),   # Pinbar
            (100, 110, 100, 106),   # Normal
        ]
        series_results = classify_series(rows)

        clf = _clf()
        manual_results = [clf.classify(o, h, l, c) for o, h, l, c in rows]

        for sr, mr in zip(series_results, manual_results):
            assert sr.candle_type == mr.candle_type
            assert sr.direction   == mr.direction
            assert sr.body_ratio  == mr.body_ratio

    def test_empty_series(self):
        assert classify_series([]) == []


# ─────────────────────────────────────────────────────────────────────────────
# 11. Absorption zone edge cases — Pine: >= and <=  (inclusive boundaries)
# ─────────────────────────────────────────────────────────────────────────────

class TestAbsorptionZone:

    def test_close_exactly_at_top_boundary_qualifies(self):
        # close = high - 0.10*totalLen exactly → inAbsZone True
        # total=10, abs_zone_top = 110-1=109. close=109 exactly.
        # body must be >= 0.50*total=5. o=104.
        r = _clf().classify(open_=104, high=110, low=100, close=109)
        assert r.candle_type == SHOCK_MOVE

    def test_close_exactly_at_bottom_boundary_qualifies(self):
        # close = low + 0.10*totalLen exactly → inAbsZone True
        # total=10, abs_zone_bot = 100+1=101. close=101.
        # body must be >= 5. o=106.
        r = _clf().classify(open_=106, high=110, low=100, close=101)
        assert r.candle_type == SHOCK_MOVE

    def test_close_one_tick_above_top_zone_is_not_in_zone(self):
        # close = 108.9 (below 109 threshold), body=5.9, ratio=0.59
        # → inAbsZone False → NOT SM
        r = _clf().classify(open_=103, high=110, low=100, close=108.9)
        assert r.candle_type == NORMAL

    def test_close_one_tick_below_bottom_zone_is_not_in_zone(self):
        # close = 101.1 (above 101 threshold), body=5.9, ratio=0.59
        # → inAbsZone False → NOT SM
        r = _clf().classify(open_=107, high=110, low=100, close=101.1)
        assert r.candle_type == NORMAL


# ─────────────────────────────────────────────────────────────────────────────
# 12. Inside Bar — Priority 5: high < prev_high AND low > prev_low
#    Added Sprint C: NCI candle proof at location (Methodology Step 8).
#    Inside bar alone is indecision — it only becomes a trade proof when
#    backed by volume / at a planned zone (road_map + business_zones).
# ─────────────────────────────────────────────────────────────────────────────

class TestInsideBar:

    def test_inside_bar_detected(self):
        """Current bar fully inside previous bar → INSIDE_BAR."""
        clf = _clf()
        # Feed a normal bar first (to set prev_high/prev_low)
        clf.classify(open_=100, high=110, low=90, close=105)
        # Current bar inside: high=108 (<110), low=92 (>90)
        r = clf.classify(open_=104, high=108, low=92, close=103)
        assert r.candle_type == INSIDE_BAR

    def test_inside_bar_not_triggered_on_first_bar(self):
        """No previous bar → cannot be inside bar."""
        clf = _clf()
        r = clf.classify(open_=100, high=110, low=90, close=105)
        assert r.candle_type != INSIDE_BAR

    def test_inside_bar_exact_boundary_not_inside(self):
        """high == prev_high or low == prev_low → NOT inside bar (strict)."""
        clf = _clf()
        clf.classify(open_=100, high=110, low=90, close=105)
        # high=110 (==prev_high) → not inside
        r = clf.classify(open_=104, high=110, low=95, close=106)
        assert r.candle_type != INSIDE_BAR

    def test_inside_bar_low_equals_prev_low_not_inside(self):
        """low == prev_low → NOT inside bar (strict)."""
        clf = _clf()
        clf.classify(open_=100, high=110, low=90, close=105)
        r = clf.classify(open_=104, high=108, low=90, close=103)
        assert r.candle_type != INSIDE_BAR

    def test_inside_bar_is_indecision(self):
        """Inside bar is an indecision candle."""
        clf = _clf()
        clf.classify(open_=100, high=110, low=90, close=105)
        r = clf.classify(open_=104, high=108, low=92, close=103)
        assert r.is_indecision is True

    def test_inside_bar_not_strong(self):
        """Inside bar does NOT feed the benchmark."""
        clf = _clf()
        _feed_strong(clf, total_len=10.0)
        clf.classify(open_=100, high=110, low=90, close=105)  # prev bar
        before = clf.avg_maru5
        r = clf.classify(open_=104, high=108, low=92, close=103)
        assert r.is_strong is False
        assert clf.avg_maru5 == before  # benchmark unchanged

    def test_inside_bar_after_reset_not_detected(self):
        """After reset, prev_high/prev_low are None → no inside bar."""
        clf = _clf()
        clf.classify(open_=100, high=110, low=90, close=105)
        clf.reset()
        r = clf.classify(open_=104, high=108, low=92, close=103)
        assert r.candle_type != INSIDE_BAR

    def test_marubozu_inside_bar_is_marubozu(self):
        """Strong candle that happens to be inside prev bar → Marubozu wins (priority)."""
        clf = _clf()
        _feed_strong(clf, total_len=100.0)  # set benchmark
        clf.classify(open_=100, high=110, low=90, close=105)  # prev bar
        # body >= 70% of total, inside prev bar → Marubozu at priority 1, not inside_bar
        # total=8 (high=108, low=100), body=7, ratio=7/8=0.875 >= 0.70
        r = clf.classify(open_=100, high=108, low=100, close=107)
        assert r.candle_type == MARUBOZU  # Priority 1 beats Priority 5

    def test_inside_bar_not_confused_with_doji(self):
        """A doji-sized bar that is also inside → Doji at priority 3 beats inside_bar."""
        clf = _clf()
        _feed_strong(clf, total_len=100.0)  # avgMaru5=100, doji threshold=30
        clf.classify(open_=100, high=110, low=90, close=105)  # prev bar
        # total=2 (< 30 = doji), inside prev bar → Doji at priority 3
        r = clf.classify(open_=100, high=102, low=100, close=100.5)
        assert r.candle_type == DOJI  # Doji beats inside_bar

    def test_series_with_mixed_inside_bars(self):
        """Verify classify_series correctly handles inside bars."""
        from core.candle_classifier import classify_series
        rows = [
            (100, 110, 90, 105),   # Normal (first bar, no prev)
            (104, 108, 92, 103),   # Inside Bar (contained by bar 1)
            (103, 107, 93, 105),   # Inside Bar (contained by bar 2)
            (103, 112, 91, 107),   # Normal (breaks above bar 3's high=107)
            (106, 111, 92, 109),   # Inside Bar (back inside bar 4)
        ]
        results = classify_series(rows)
        assert results[0].candle_type == NORMAL
        assert results[1].candle_type == INSIDE_BAR
        assert results[2].candle_type == INSIDE_BAR
        assert results[3].candle_type == NORMAL
        assert results[4].candle_type == INSIDE_BAR


if __name__ == "__main__":
    # Repo convention is `python tests/test_X.py` — without this guard,
    # running this pytest-class file that way silently executes zero tests.
    sys.exit(pytest.main([__file__, "-q"]))
