"""
Tests for core/range_detector.py  (BreakoutTracker)
=====================================================
Rules being tested:

  1. Trigger  : A Marubozu or ShockMove with direction "bull"/"bear" fires
                a "triggered" event and opens the 4-bar window.

  2. Confirm  : Any bar within the window that closes beyond trigger_close
                and is NOT a Doji fires a "confirmed" event.
                Bull: confirm_close > trigger_close
                Bear: confirm_close < trigger_close

  3. Fail     : If the window reaches pa_window bars with no confirmation,
                a "failed" event fires.

  4. One at a time : A new Maru/SM while a window is open is ignored.

  5. Doji guard : Doji cannot be the confirming bar (NCI: no power).

  6. Window boundary : Bar at offset == pa_window is still valid.
                       Bar at offset > pa_window is too late (expired).

  7. Flat Maru (direction=none) : does NOT open a window.
"""

import pytest
from core.range_detector import BreakoutTracker, BreakoutEvent
from core.candle_classifier import MARUBOZU, SHOCK_MOVE, DOJI, PINBAR, NORMAL


def _tracker(pa_window=4):
    return BreakoutTracker(pa_window=pa_window)


def _feed(tracker, bars):
    """Feed (bar_index, close, is_strong, direction, candle_type) tuples."""
    events = []
    for b in bars:
        events += tracker.update(*b)
    return events


# ─────────────────────────────────────────────────────────────────────────────
# 1. Trigger
# ─────────────────────────────────────────────────────────────────────────────

class TestTrigger:

    def test_bull_maru_fires_triggered(self):
        t = _tracker()
        evs = t.update(0, close=115.0, is_strong=True, direction="bull", candle_type=MARUBOZU)
        assert len(evs) == 1
        assert evs[0].kind == "triggered"
        assert evs[0].is_up is True
        assert evs[0].trigger_close == 115.0

    def test_bear_maru_fires_triggered(self):
        t = _tracker()
        evs = t.update(0, close=95.0, is_strong=True, direction="bear", candle_type=MARUBOZU)
        assert evs[0].kind == "triggered"
        assert evs[0].is_up is False

    def test_shock_move_fires_triggered(self):
        t = _tracker()
        evs = t.update(0, close=115.0, is_strong=True, direction="bull", candle_type=SHOCK_MOVE)
        assert evs[0].kind == "triggered"

    def test_flat_maru_no_trigger(self):
        # direction="none" should not open a window
        t = _tracker()
        evs = t.update(0, close=100.0, is_strong=True, direction="none", candle_type=MARUBOZU)
        assert evs == []
        assert t.window_open is False

    def test_normal_candle_no_trigger(self):
        t = _tracker()
        evs = t.update(0, close=115.0, is_strong=False, direction="bull", candle_type=NORMAL)
        assert evs == []

    def test_window_open_after_trigger(self):
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)
        assert t.window_open is True
        assert t.trigger_close == 115.0

    def test_trigger_bar_recorded(self):
        t = _tracker()
        t.update(5, 115.0, True, "bull", MARUBOZU)
        evs = t.update(6, 116.0, False, "bull", NORMAL)  # confirm
        confirmed = [e for e in evs if e.kind == "confirmed"]
        assert confirmed[0].trigger_bar == 5


# ─────────────────────────────────────────────────────────────────────────────
# 2. Confirmation
# ─────────────────────────────────────────────────────────────────────────────

class TestConfirmation:

    def test_bull_confirm_on_next_bar(self):
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)
        evs = t.update(1, 116.0, False, "bull", NORMAL)  # close > 115 = confirmed
        confirmed = [e for e in evs if e.kind == "confirmed"]
        assert len(confirmed) == 1
        assert confirmed[0].confirm_close == 116.0
        assert confirmed[0].confirm_bar == 1

    def test_bear_confirm_on_next_bar(self):
        t = _tracker()
        t.update(0, 95.0, True, "bear", MARUBOZU)
        evs = t.update(1, 94.0, False, "bear", NORMAL)  # close < 95 = confirmed
        assert any(e.kind == "confirmed" for e in evs)

    def test_confirm_resets_window(self):
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)
        t.update(1, 116.0, False, "bull", NORMAL)
        assert t.window_open is False

    def test_confirm_exactly_at_trigger_close_does_not_confirm(self):
        # Must be STRICTLY beyond (>  not >=)
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)
        evs = t.update(1, 115.0, False, "bull", NORMAL)  # close == trigger_close
        assert not any(e.kind == "confirmed" for e in evs)

    def test_confirm_on_second_bar(self):
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)
        t.update(1, 114.0, False, "bull", NORMAL)   # below — no confirm
        evs = t.update(2, 116.0, False, "bull", NORMAL)  # above — confirm
        assert any(e.kind == "confirmed" for e in evs)

    def test_confirm_on_fourth_bar(self):
        # Last valid bar in window
        t = _tracker(pa_window=4)
        t.update(0, 115.0, True, "bull", MARUBOZU)
        t.update(1, 114.0, False, "bull", NORMAL)
        t.update(2, 114.0, False, "bull", NORMAL)
        t.update(3, 114.0, False, "bull", NORMAL)
        evs = t.update(4, 116.0, False, "bull", NORMAL)  # offset=4 == pa_window
        assert any(e.kind == "confirmed" for e in evs)

    def test_pinbar_can_confirm(self):
        # Pinbar is NOT a Doji — it can confirm
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)
        evs = t.update(1, 116.0, False, "bull", PINBAR)
        assert any(e.kind == "confirmed" for e in evs)

    def test_strong_bar_can_confirm(self):
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)
        evs = t.update(1, 116.0, True, "bull", MARUBOZU)
        assert any(e.kind == "confirmed" for e in evs)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Failure (window expiry)
# ─────────────────────────────────────────────────────────────────────────────

class TestFailure:

    def test_failed_after_pa_window_bars(self):
        t = _tracker(pa_window=4)
        t.update(0, 115.0, True, "bull", MARUBOZU)
        t.update(1, 114.0, False, "bull", NORMAL)
        t.update(2, 114.0, False, "bull", NORMAL)
        t.update(3, 114.0, False, "bull", NORMAL)
        evs = t.update(4, 114.0, False, "bull", NORMAL)  # offset=4, no confirm
        assert any(e.kind == "failed" for e in evs)

    def test_failed_event_has_correct_fields(self):
        t = _tracker(pa_window=2)
        t.update(0, 115.0, True, "bull", MARUBOZU)
        t.update(1, 114.0, False, "bull", NORMAL)
        evs = t.update(2, 114.0, False, "bull", NORMAL)
        failed = next(e for e in evs if e.kind == "failed")
        assert failed.is_up is True
        assert failed.trigger_bar == 0
        assert failed.trigger_close == 115.0
        assert failed.confirm_bar is None

    def test_window_closed_after_failure(self):
        t = _tracker(pa_window=1)
        t.update(0, 115.0, True, "bull", MARUBOZU)
        t.update(1, 114.0, False, "bull", NORMAL)  # offset=1=pa_window, no confirm
        assert t.window_open is False

    def test_new_trigger_possible_after_failure(self):
        t = _tracker(pa_window=1)
        t.update(0, 115.0, True, "bull", MARUBOZU)
        t.update(1, 114.0, False, "bull", NORMAL)  # fails
        # Now a new Maru should open a fresh window
        evs = t.update(2, 120.0, True, "bull", MARUBOZU)
        assert any(e.kind == "triggered" for e in evs)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Doji guard
# ─────────────────────────────────────────────────────────────────────────────

class TestDojiGuard:

    def test_doji_cannot_confirm(self):
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)
        evs = t.update(1, 116.0, False, "bull", DOJI)  # close is beyond but Doji
        assert not any(e.kind == "confirmed" for e in evs)

    def test_doji_counts_toward_window_expiry(self):
        # Two bars: bar 1 is Doji (no confirm), bar 2 confirms normally
        t = _tracker(pa_window=2)
        t.update(0, 115.0, True, "bull", MARUBOZU)
        t.update(1, 116.0, False, "bull", DOJI)   # no confirm
        evs = t.update(2, 116.0, False, "bull", NORMAL)  # offset=2=pa_window, confirms
        assert any(e.kind == "confirmed" for e in evs)

    def test_all_doji_window_expires(self):
        t = _tracker(pa_window=2)
        t.update(0, 115.0, True, "bull", MARUBOZU)
        t.update(1, 116.0, False, "bull", DOJI)
        evs = t.update(2, 116.0, False, "bull", DOJI)
        # offset=2=pa_window, Doji cannot confirm → expired
        assert any(e.kind == "failed" for e in evs)


# ─────────────────────────────────────────────────────────────────────────────
# 5. One window at a time
# ─────────────────────────────────────────────────────────────────────────────

class TestOneWindowAtATime:

    def test_new_maru_during_window_is_ignored(self):
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)   # trigger, window open
        evs = t.update(1, 120.0, True, "bull", MARUBOZU)  # new Maru — ignored
        # The new Maru is also a close > 115, so it will CONFIRM, not re-trigger
        # Since 120 > 115 (trigger_close), it confirms the existing window
        assert any(e.kind == "confirmed" for e in evs)
        assert not any(e.kind == "triggered" for e in evs)

    def test_after_confirm_new_trigger_works(self):
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)
        t.update(1, 116.0, False, "bull", NORMAL)  # confirm
        evs = t.update(2, 120.0, True, "bull", MARUBOZU)
        assert any(e.kind == "triggered" for e in evs)

    def test_bear_maru_during_bull_window_ignored(self):
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)  # bull window
        evs = t.update(1, 90.0, True, "bear", MARUBOZU)  # bear Maru during bull window
        # close=90 < trigger_close=115 — does NOT confirm bull window
        # Also should not open a bear window
        assert not any(e.kind == "confirmed" for e in evs)
        assert t.window_open is True
        assert t.is_up is True   # still the original bull window


# ─────────────────────────────────────────────────────────────────────────────
# 6. Reset
# ─────────────────────────────────────────────────────────────────────────────

class TestReset:

    def test_hard_reset_clears_window(self):
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)
        assert t.window_open is True
        t.reset()
        assert t.window_open is False
        assert t.trigger_close is None

    def test_can_trigger_after_reset(self):
        t = _tracker()
        t.update(0, 115.0, True, "bull", MARUBOZU)
        t.reset()
        evs = t.update(1, 120.0, True, "bull", MARUBOZU)
        assert any(e.kind == "triggered" for e in evs)
