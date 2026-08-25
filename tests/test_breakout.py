"""
Tests for core/breakout.py
============================
Every threshold, condition name, and edge case traces directly to a line in
trading/nci_range_breakout.pine.

Pine Script reference (condensed):
  Standard A: f_isMaOrSM(0) and f_isMaOrSM(1)
                _aSize  = na(mx) or (tl0>=0.60*mx and tl1>=0.60*mx)
                _sameDir = both bull (up) or both bear (down)
                _prevOut = c1 close outside boundary
                condA = c0 extends further than c1
                condB = tl0 >= 0.70 * tl1
                f=0 → confirmed,  f=1 → pending

  Standard B: f_isMaOrSM(1) (only prev needs to be strong)
                c1 dir correct, c1 outside boundary
                condD = body_past_bnd[1] / body[1] >= 0.30
                condA = tl1 > mx
                condB = tl0 <= 0.30 * tl1
                condC = c0 not deep in c1 (midpoint rule)
                f=0 → confirmed,  f=1 → pending

  Standard C: f_isMaOrSM(0)
                c0 dir correct, 30%+ body past boundary, tl0 >= 0.70*mx
                always → pending

  PA window: close on correct side of bo_line AND beyond ext AND not Doji
"""

import pytest
from core.breakout import (
    evaluate_breakout,
    PAWindow,
    BreakoutOutcome,
    _body_past_boundary,
    _effective_max,
    _std_a,
    _std_b,
    _std_c,
)
from core.candle_classifier import MARUBOZU, SHOCK_MOVE, DOJI, PINBAR, NORMAL


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bo(
    # Minimal upward two-Maru Standard A confirmed
    c0_open=100, c0_high=120, c0_low=100, c0_close=114,   # total=20 body=14 ratio=0.70
    c0_is_strong=True,
    c1_open=100, c1_high=120, c1_low=100, c1_close=114,   # same
    c1_is_strong=True,
    boundary=110.0,
    is_up=True,
    max_maru5=None,
) -> BreakoutOutcome:
    return evaluate_breakout(
        c0_open, c0_high, c0_low, c0_close, c0_is_strong,
        c1_open, c1_high, c1_low, c1_close, c1_is_strong,
        boundary, is_up, max_maru5,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Standard A — two consecutive MaOrSM
# ─────────────────────────────────────────────────────────────────────────────

class TestStandardA:

    def test_both_conditions_met_is_confirmed(self):
        # Up breakout.  boundary=110.
        # c1: o=100 h=120 l=100 c=114  total=20  body=14  ratio=0.70 → strong
        #     c1 close=114 > boundary=110 ✓ (_prevOut)
        #     c1 bull (114>100) ✓
        # c0: o=110 h=130 l=110 c=126  total=20  body=16  ratio=0.80 → strong
        #     c0 close=126 > c1 close=114 ✓ (condA: extends further)
        #     c0 total=20 >= 0.70*20=14 ✓ (condB)
        # max_maru5=None → size guard skipped
        r = evaluate_breakout(
            110, 130, 110, 126, True,
            100, 120, 100, 114, True,
            boundary=110.0, is_up=True, max_maru5=None,
        )
        assert r.outcome == "confirmed"
        assert r.standard == "A"
        assert r.is_up is True

    def test_one_condition_fails_is_pending(self):
        # condA fails: c0 close = 113 < c1 close = 114 (does NOT extend further)
        # condB passes: c0 total=20 >= 0.70*20=14
        r = evaluate_breakout(
            100, 120, 100, 113, True,   # c0: close=113, less than c1 close=114
            100, 120, 100, 114, True,   # c1: close=114 > boundary=110
            boundary=110.0, is_up=True, max_maru5=None,
        )
        assert r.outcome == "pending"
        assert r.standard == "A"

    def test_both_conditions_fail_is_none(self):
        # condA fails: c0 close < c1 close (does not extend further)
        # condB fails: c0 total=5 < 0.70*20=14
        # Standard A skips. Standard B: b_past=4/14=0.286 < 0.30 → also skips.
        # Standard C: c0 total=5 < 0.70*max_maru5=14 → size guard fails → skips.
        # → outcome is "none"
        r = evaluate_breakout(
            110, 115, 110, 113, True,   # c0: total=5 (tiny), close=113 < 114
            100, 120, 100, 114, True,
            boundary=110.0, is_up=True, max_maru5=20.0,  # c0 total=5 < 0.70*20=14 → C size fails
        )
        assert r.outcome == "none"

    def test_c0_not_strong_skips_std_a(self):
        # c0 not strong → Standard A cannot fire
        r = evaluate_breakout(
            100, 120, 100, 114, False,  # c0 not strong
            100, 120, 100, 114, True,
            boundary=110.0, is_up=True, max_maru5=None,
        )
        # Falls through to B or C or none
        assert r.standard != "A"

    def test_c1_not_strong_skips_std_a(self):
        r = evaluate_breakout(
            100, 120, 100, 114, True,
            100, 120, 100, 114, False,  # c1 not strong
            boundary=110.0, is_up=True, max_maru5=None,
        )
        assert r.standard != "A"

    def test_c1_close_not_outside_boundary_skips_std_a(self):
        # c1 close = 109 < boundary=110 → _prevOut fails
        r = evaluate_breakout(
            100, 120, 100, 115, True,
            100, 120, 100, 109, True,   # c1 close not outside
            boundary=110.0, is_up=True, max_maru5=None,
        )
        assert r.standard != "A"

    def test_size_guard_both_must_be_60pct_of_max(self):
        # max_maru5=100. Both must be >= 60 (total).
        # c0 total=55 (< 60) → size guard fails → Standard A skipped
        r = evaluate_breakout(
            100, 155, 100, 140, True,   # c0 total=55
            100, 200, 100, 185, True,   # c1 total=100
            boundary=110.0, is_up=True, max_maru5=100.0,
        )
        # A is skipped due to size; B may or may not fire but A must not
        assert r.standard != "A"

    def test_bearish_breakout_confirmed(self):
        # Down breakout. boundary=110.
        # c1: o=120 h=120 l=100 c=106  bear, total=20 body=14 → strong
        #     c1 close=106 < boundary=110 ✓
        # c0: o=115 h=115 l=95 c=101   bear, total=20 body=14 → strong
        #     c0 close=101 < c1 close=106 ✓ (extends further down)
        #     c0 total=20 >= 0.70*20=14 ✓
        r = evaluate_breakout(
            115, 115, 95, 101, True,
            120, 120, 100, 106, True,
            boundary=110.0, is_up=False, max_maru5=None,
        )
        assert r.outcome == "confirmed"
        assert r.standard == "A"
        assert r.is_up is False

    def test_sameDir_fails_when_mixed_direction(self):
        # c1 is bullish but c0 is bearish → sameDir fails
        r = evaluate_breakout(
            115, 120, 100, 101, True,   # c0: bear
            100, 120, 100, 114, True,   # c1: bull
            boundary=110.0, is_up=True, max_maru5=None,
        )
        assert r.standard != "A"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Standard B — large MaOrSM[1] with 30%+ body past boundary
# ─────────────────────────────────────────────────────────────────────────────

class TestStandardB:

    def _base_std_b_confirmed(self):
        """Construct a clean Standard B scenario — all three conditions met.

        boundary=100.  is_up=True.  max_maru5=18 (so tl1=20 > 18 → condA ✓).
        c1: o=90  h=120 l=90  c=115  total=30  body=25  bull  close=115>100
            body_past = c=115 - max(o=90, bnd=100) = 115-100 = 15
            15/25 = 0.60 >= 0.30 (condD ✓)  Note: >60% of body past boundary
        c0: o=112 h=118 l=113 c=116  total=5 (5 <= 0.30*30=9 ✓ condB)
            c0 low=113 > c1 low=90 + 0.50*30=105 ✓ (condC: 113>105)
        condA: tl1=30 > max_maru5=18 ✓
        """
        return evaluate_breakout(
            112, 118, 113, 116, False,   # c0: NOT strong (small follow)
            90, 120, 90, 115, True,      # c1: strong bull
            boundary=100.0, is_up=True, max_maru5=18.0,
        )

    def test_all_three_conditions_met_is_confirmed(self):
        r = self._base_std_b_confirmed()
        assert r.outcome == "confirmed"
        assert r.standard == "B"

    def test_one_condition_fails_is_pending(self):
        # condA fails: max_maru5=40 so tl1=30 NOT > 40
        r = evaluate_breakout(
            112, 118, 113, 116, False,
            90, 120, 90, 115, True,
            boundary=100.0, is_up=True, max_maru5=40.0,   # condA fails
        )
        assert r.outcome == "pending"
        assert r.standard == "B"

    def test_two_conditions_fail_is_none(self):
        # condA fails (max_maru5=40) AND condB fails (c0 total=20, 0.30*30=9, 20>9)
        r = evaluate_breakout(
            100, 120, 100, 115, False,  # c0: total=20 (too large)
            90, 120, 90, 115, True,
            boundary=100.0, is_up=True, max_maru5=40.0,
        )
        assert r.outcome == "none"

    def test_c1_not_strong_skips_std_b(self):
        r = evaluate_breakout(
            112, 118, 113, 116, False,
            90, 120, 90, 115, False,   # c1 not strong
            boundary=100.0, is_up=True, max_maru5=18.0,
        )
        assert r.standard != "B"

    def test_body_past_boundary_less_than_30pct_skips_std_b(self):
        # c1: body_past=5 out of body=25 → 5/25=0.20 < 0.30 (condD fails)
        # c1: o=96, c=116, body=20, bnd=100. body_past = 116-max(96,100) = 116-100=16
        # That's 16/20=0.80 — need to FAIL. Try:
        # c1: o=85, c=112, body=27, bnd=100. body_past=112-max(85,100)=112-100=12. 12/27=0.44 ≥ 0.30
        # To get < 0.30 we need little body past boundary.
        # c1: o=85, c=107, body=22, bnd=100. body_past=107-100=7. 7/22=0.318 barely passes.
        # c1: o=85, c=106, body=21, bnd=100. body_past=106-100=6. 6/21=0.286 < 0.30 ✓ fails condD
        r = evaluate_breakout(
            102, 108, 102, 105, False,
            85, 120, 85, 106, True,    # body=21, body_past=6 → 6/21=0.286 < 0.30
            boundary=100.0, is_up=True, max_maru5=18.0,
        )
        assert r.standard != "B"

    def test_condC_midpoint_fails_when_c0_low_too_deep(self):
        # condC: c0 low > c1 low + 0.50*tl1  → fails when c0 dips into lower half of c1
        # c1: l=90, tl1=30. midpoint = 90 + 15 = 105. c0 low must be > 105.
        # Set c0 low = 99 (below 105) → condC fails
        r = evaluate_breakout(
            112, 118, 99, 116, False,  # c0 low=99 < 105 → condC fails
            90, 120, 90, 115, True,
            boundary=100.0, is_up=True, max_maru5=18.0,
        )
        # condA ✓, condB (total=19, 0.30*30=9, 19>9 → condB fails), condC fails → 2 fails → none
        assert r.outcome == "none"

    def test_bearish_std_b_confirmed(self):
        # Down breakout. boundary=100.
        # c1: o=110 h=110 l=80 c=85  bear, total=30, body=25
        #     c1 close=85 < boundary=100 ✓, c1_dir bear ✓
        #     body_past = min(open=110, bnd=100) - close=85 = 100-85=15. 15/25=0.60 ✓
        # c0: o=88 h=87 l=82 c=84   total=5 (<=0.30*30=9 ✓), high=87 < high[1] - 0.50*30=95 ✓
        # condA: tl1=30 > max_maru5=18 ✓
        r = evaluate_breakout(
            88, 87, 82, 84, False,
            110, 110, 80, 85, True,
            boundary=100.0, is_up=False, max_maru5=18.0,
        )
        assert r.outcome == "confirmed"
        assert r.standard == "B"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Standard C — single strong candle, always pending
# ─────────────────────────────────────────────────────────────────────────────

class TestStandardC:

    def test_single_strong_candle_with_30pct_body_past_boundary_is_pending(self):
        # boundary=100, is_up=True. max_maru5=20.
        # c0: o=95, h=120, l=95, c=115. total=25>=0.70*20=14 ✓
        #     body=20. body_past=115-max(95,100)=115-100=15. 15/20=0.75>=0.30 ✓
        #     c0 dir: bull ✓.  c0 is_strong ✓.
        # c1 not strong → Standards A and B skip.
        r = evaluate_breakout(
            95, 120, 95, 115, True,    # c0: strong bull, clears boundary cleanly
            100, 110, 100, 105, False, # c1: not strong (Normal)
            boundary=100.0, is_up=True, max_maru5=20.0,
        )
        assert r.outcome == "pending"
        assert r.standard == "C"

    def test_std_c_always_pending_never_confirmed(self):
        # Even a perfect single candle is always "pending" for Standard C
        r = evaluate_breakout(
            95, 120, 95, 115, True,
            100, 110, 100, 105, False,
            boundary=100.0, is_up=True, max_maru5=None,  # no size guard
        )
        assert r.outcome != "confirmed"

    def test_std_c_wrong_direction_skips(self):
        # is_up=True but c0 is bearish
        r = evaluate_breakout(
            115, 120, 95, 100, True,   # c0: close=100 < open=115 (bear)
            100, 110, 100, 105, False,
            boundary=100.0, is_up=True, max_maru5=20.0,
        )
        assert r.standard != "C"

    def test_std_c_less_than_30pct_body_past_skips(self):
        # body=20, body_past needs < 30% = < 6.
        # c0: o=90, c=108. body=18. body_past=108-max(90,100)=108-100=8. 8/18=0.44 ≥ 0.30
        # Need body_past < 6. c0: o=90, c=105. body=15. body_past=105-100=5. 5/15=0.333 > 0.30 — still passes.
        # c0: o=94, c=103. body=9. body_past=103-100=3. 3/9=0.333 — passes again.
        # c0: o=97, c=102. body=5. body_past=102-100=2. 2/5=0.40 — passes.
        # c0: o=99, c=102. body=3. body_past=102-100=2. 2/3=0.667 — passes.
        # Need body where body_past/body < 0.30:
        # open ABOVE boundary so body_past = close - open (if open>bnd):
        # c0: o=101, h=120, l=100, c=106. open=101>boundary=100. body=5, close=106.
        #   body_past = close - max(open, bnd) = 106-101=5. 5/5=1.0 — passes.
        # The issue is when open is above boundary, all body is past it.
        # Try open BELOW boundary: o=95, h=120, l=95, c=101. body=6. body_past=101-100=1. 1/6=0.167 < 0.30 ✓
        r = evaluate_breakout(
            95, 120, 95, 101, True,    # body=6, body_past=1, 16.7% < 30%
            100, 110, 100, 105, False,
            boundary=100.0, is_up=True, max_maru5=20.0,
        )
        assert r.standard != "C"
        assert r.outcome == "none"

    def test_std_c_size_guard_fails_when_too_small(self):
        # max_maru5=100. c0 total must be >= 70. Here total=25 < 70 → fails.
        r = evaluate_breakout(
            95, 120, 95, 115, True,    # total=25 < 0.70*100=70
            100, 110, 100, 105, False,
            boundary=100.0, is_up=True, max_maru5=100.0,
        )
        assert r.standard != "C"

    def test_std_c_ext_uses_only_c0(self):
        # Pine: boExtHigh = high[0], boExtLow = low[0] for Standard C
        r = evaluate_breakout(
            95, 120, 95, 115, True,
            100, 150, 100, 105, False,  # c1 has high=150 but should not affect ext
            boundary=100.0, is_up=True, max_maru5=20.0,
        )
        assert r.standard == "C"
        assert r.ext_high == 120   # c0 high, not c1 high (150)
        assert r.ext_low  == 95    # c0 low


# ─────────────────────────────────────────────────────────────────────────────
# 4. Priority order — A fires before B, B before C
# ─────────────────────────────────────────────────────────────────────────────

class TestPriority:

    def test_a_confirmed_prevents_b_and_c(self):
        # Both A and B would trigger; A fires first
        r = evaluate_breakout(
            110, 130, 110, 126, True,
            100, 120, 100, 114, True,
            boundary=110.0, is_up=True, max_maru5=None,
        )
        assert r.standard == "A"

    def test_a_pending_prevents_b_and_c(self):
        # A is pending (one condition fails). B might also qualify but A is checked first.
        # Make A pending: condA fails (c0 close < c1 close)
        r = evaluate_breakout(
            100, 120, 100, 113, True,  # c0 close=113 < c1 close=114
            100, 120, 100, 114, True,
            boundary=110.0, is_up=True, max_maru5=None,
        )
        assert r.standard == "A"
        assert r.outcome == "pending"

    def test_c_only_fires_when_a_and_b_both_skip(self):
        # c0 strong but c1 not strong → A skips (c1 not strong), B skips (c1 not strong)
        # c0 meets Standard C requirements → Standard C pending
        r = evaluate_breakout(
            95, 120, 95, 115, True,
            100, 110, 100, 105, False,  # c1 not strong
            boundary=100.0, is_up=True, max_maru5=20.0,
        )
        assert r.standard == "C"

    def test_none_when_all_standards_miss(self):
        r = evaluate_breakout(
            100, 110, 100, 106, False,  # c0: not strong
            100, 110, 100, 106, False,  # c1: not strong
            boundary=100.0, is_up=True, max_maru5=None,
        )
        assert r.outcome == "none"
        assert r.standard == ""


# ─────────────────────────────────────────────────────────────────────────────
# 5. PA Window
# ─────────────────────────────────────────────────────────────────────────────

class TestPAWindow:

    def _window(self, is_up=True) -> PAWindow:
        return PAWindow(
            bo_line=100.0,
            ext_high=115.0,
            ext_low=95.0,
            is_up=is_up,
            window=4,
        )

    def test_confirmation_on_first_bar(self):
        w = self._window(is_up=True)
        result = w.check(NORMAL, close=116.0, bar_offset=1)
        assert result is True
        assert w.confirmed is True

    def test_confirmation_close_must_exceed_ext_high(self):
        w = self._window(is_up=True)
        # close=114 is above bo_line=100 but NOT above ext_high=115
        result = w.check(NORMAL, close=114.0, bar_offset=1)
        assert result is False

    def test_confirmation_close_must_exceed_bo_line(self):
        w = self._window(is_up=True)
        # close=99 is below bo_line=100
        result = w.check(NORMAL, close=99.0, bar_offset=1)
        assert result is False

    def test_doji_cannot_confirm(self):
        # f_isValidConf in Pine excludes Doji
        w = self._window(is_up=True)
        result = w.check(DOJI, close=120.0, bar_offset=1)
        assert result is False

    def test_marubozu_can_confirm(self):
        w = self._window(is_up=True)
        result = w.check(MARUBOZU, close=120.0, bar_offset=1)
        assert result is True

    def test_pinbar_can_confirm(self):
        # f_isValidConf = not Doji → Pinbar is valid
        w = self._window(is_up=True)
        result = w.check(PINBAR, close=120.0, bar_offset=1)
        assert result is True

    def test_window_expires_after_pa_window_bars(self):
        # Default window=4. bar_offset=5 → expired
        w = self._window()
        result = w.check(MARUBOZU, close=120.0, bar_offset=5)
        assert result is False
        assert w.expired is True

    def test_window_exactly_at_pa_window_is_still_valid(self):
        # bar_offset=4 == window=4 → still valid
        w = self._window()
        result = w.check(NORMAL, close=120.0, bar_offset=4)
        assert result is True

    def test_no_calls_after_confirmed(self):
        w = self._window()
        w.check(NORMAL, 120.0, bar_offset=1)   # confirms
        assert w.confirmed is True
        result = w.check(NORMAL, 120.0, bar_offset=2)
        assert result is False  # already confirmed, no double-fire

    def test_no_calls_after_expired(self):
        w = self._window()
        w.check(NORMAL, 120.0, bar_offset=5)   # expires
        assert w.expired is True
        result = w.check(MARUBOZU, 120.0, bar_offset=6)
        assert result is False

    def test_bearish_confirmation(self):
        w = self._window(is_up=False)
        # close must be < bo_line=100 AND < ext_low=95
        result = w.check(NORMAL, close=94.0, bar_offset=1)
        assert result is True

    def test_bearish_close_above_ext_low_fails(self):
        w = self._window(is_up=False)
        # close=96 < bo_line=100 but NOT < ext_low=95
        result = w.check(NORMAL, close=96.0, bar_offset=1)
        assert result is False

    def test_confirmation_on_last_valid_bar(self):
        w = self._window()
        # bar 1,2,3 fail, bar 4 confirms
        w.check(DOJI,   120.0, bar_offset=1)
        w.check(DOJI,   120.0, bar_offset=2)
        w.check(DOJI,   120.0, bar_offset=3)
        result = w.check(NORMAL, 120.0, bar_offset=4)
        assert result is True
        assert w.confirmed is True


# ─────────────────────────────────────────────────────────────────────────────
# 6. BreakoutOutcome ext_high / ext_low values
# ─────────────────────────────────────────────────────────────────────────────

class TestExtents:

    def test_std_a_ext_uses_max_of_both_candles(self):
        # Pine: boExtHigh = max(high[1], high[0]) for A and B
        r = evaluate_breakout(
            110, 130, 110, 126, True,   # c0 high=130
            100, 125, 100, 114, True,   # c1 high=125
            boundary=110.0, is_up=True, max_maru5=None,
        )
        assert r.standard == "A"
        assert r.ext_high == 130    # max(125, 130)
        assert r.ext_low  == 100    # min(110, 100)

    def test_std_c_ext_uses_only_c0(self):
        r = evaluate_breakout(
            95, 120, 95, 115, True,     # c0 high=120
            100, 200, 100, 105, False,  # c1 high=200 — should be ignored
            boundary=100.0, is_up=True, max_maru5=20.0,
        )
        assert r.standard == "C"
        assert r.ext_high == 120   # c0 high only
        assert r.ext_low  == 95    # c0 low only


# ─────────────────────────────────────────────────────────────────────────────
# 7. Helper function unit tests (_body_past_boundary, _effective_max)
# ─────────────────────────────────────────────────────────────────────────────

class TestHelpers:

    # _body_past_boundary
    def test_body_past_up_when_close_above_boundary(self):
        # Pine (up): max(close - max(open, bnd), 0)
        # o=90, c=115, bnd=100. max(115 - max(90,100), 0) = max(115-100, 0) = 15
        assert _body_past_boundary(90, 115, 100, True) == pytest.approx(15.0)

    def test_body_past_up_when_open_above_boundary(self):
        # o=105, c=115, bnd=100. max(115 - max(105,100), 0) = max(115-105, 0) = 10
        assert _body_past_boundary(105, 115, 100, True) == pytest.approx(10.0)

    def test_body_past_up_is_zero_when_close_below_boundary(self):
        # c=95 < bnd=100. max(95-max(90,100), 0) = max(-5, 0) = 0
        assert _body_past_boundary(90, 95, 100, True) == pytest.approx(0.0)

    def test_body_past_down_when_close_below_boundary(self):
        # Pine (down): max(min(open, bnd) - close, 0)
        # o=110, c=85, bnd=100. max(min(110,100) - 85, 0) = max(100-85, 0) = 15
        assert _body_past_boundary(110, 85, 100, False) == pytest.approx(15.0)

    def test_body_past_down_when_open_below_boundary(self):
        # o=95, c=85, bnd=100. max(min(95,100) - 85, 0) = max(95-85, 0) = 10
        assert _body_past_boundary(95, 85, 100, False) == pytest.approx(10.0)

    def test_body_past_down_is_zero_when_close_above_boundary(self):
        assert _body_past_boundary(110, 105, 100, False) == pytest.approx(0.0)

    # _effective_max
    def test_effective_max_uses_max_maru5_when_available(self):
        assert _effective_max(50.0, 30.0, 20.0) == 50.0

    def test_effective_max_falls_back_to_tl1_when_max_is_none(self):
        assert _effective_max(None, 30.0, 20.0) == 30.0

    def test_effective_max_falls_back_to_tl0_when_tl1_is_zero(self):
        assert _effective_max(None, 0.0, 20.0) == 20.0

    def test_effective_max_returns_none_when_all_zero(self):
        assert _effective_max(None, 0.0, 0.0) is None
