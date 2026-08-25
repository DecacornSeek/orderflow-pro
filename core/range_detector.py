"""
NCI Breakout Tracker - simplified NCI breakout + PA confirmation logic.

SOURCE OF TRUTH
---------------
trading/nci_range_breakout.pine  +  Technical Requirements - candles and breakout.txt

HOW IT WORKS
------------
The full Pine range-state machine (IDLE -> WATCHING -> ACTIVE) is deliberately
omitted here.  The live signal pipeline handles higher-timeframe context
externally (session filters, HTF ranging flag).  This module focuses on the
one thing that generates a trade decision:

  1. A MARUBOZU or SHOCK_MOVE candle closes with clear direction.
     That candle IS the breakout trigger - its close is the level that matters.

  2. A 4-bar Price Action Confirmation (PAC) window opens on the NEXT bar.
     Any one bar within the window that closes BEYOND the trigger close
     (above for bull, below for bear) and is NOT a Doji = CONFIRMED.

  3. If the window expires with no such bar = FAILED.  Reset and wait for
     the next strong candle.

Only one window is open at a time.  A new Maru/SM while a window is already
open is ignored - the existing window runs to completion first.

CONFIRMATION RULE
-----------------
  Bull: confirm_close > trigger_close  (market accepted the move higher)
  Bear: confirm_close < trigger_close  (market accepted the move lower)
  Doji cannot confirm (NCI: no power).

USAGE
-----
    from core.candle_classifier import CandleClassifier
    from core.range_detector import BreakoutTracker

    clf = CandleClassifier()
    tracker = BreakoutTracker(pa_window=4)

    for bar_index, (o, h, l, c) in enumerate(ohlc_bars):
        result = clf.classify(open_=o, high=h, low=l, close=c)
        events = tracker.update(
            bar_index=bar_index,
            close=c,
            is_strong=result.is_strong,
            direction=result.direction,
            candle_type=result.candle_type,
        )
        for ev in events:
            print(ev)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.candle_classifier import DOJI

PA_WINDOW_DEFAULT = 4   # bars to wait for confirmation (NCI "Next 4 Candles")


@dataclass(frozen=True)
class BreakoutEvent:
    """Emitted by BreakoutTracker on every noteworthy state change.

    kind            "triggered"   - a Maru/SM just fired; window is now open.
                    "confirmed"   - a PAC bar closed beyond trigger_close.
                    "failed"      - window expired with no confirmation.

    is_up           True = bull breakout, False = bear.
    trigger_bar     bar_index of the Maru/SM that started the window.
    trigger_close   Close price of the trigger candle (the level to beat).
    confirm_bar     bar_index of the confirming bar (None for triggered/failed).
    confirm_close   Close price of the confirming bar (None for triggered/failed).
    """
    kind:           str
    is_up:          bool
    trigger_bar:    int
    trigger_close:  float
    confirm_bar:    Optional[int]   = None
    confirm_close:  Optional[float] = None


class BreakoutTracker:
    """Stateful NCI breakout + PAC tracker.

    Feed bars one at a time via update() in chronological order.
    Returns a (possibly empty) list of BreakoutEvent per bar.

    Parameters
    ----------
    pa_window : Number of bars to wait for PA confirmation.  NCI default: 4.
    """

    def __init__(self, pa_window: int = PA_WINDOW_DEFAULT) -> None:
        self._pa_window = pa_window
        self._reset()

    def update(
        self,
        bar_index:   int,
        close:       float,
        is_strong:   bool,
        direction:   str,     # "bull" | "bear" | "none"
        candle_type: str,
    ) -> list[BreakoutEvent]:
        """Process one confirmed bar. Returns a (possibly empty) list of events."""
        events: list[BreakoutEvent] = []

        if self._window_open:
            offset = bar_index - self._trigger_bar

            if offset <= self._pa_window:
                is_valid = candle_type != DOJI
                if self._is_up:
                    confirmed = is_valid and close > self._trigger_close
                else:
                    confirmed = is_valid and close < self._trigger_close

                if confirmed:
                    events.append(BreakoutEvent(
                        kind="confirmed",
                        is_up=self._is_up,
                        trigger_bar=self._trigger_bar,
                        trigger_close=self._trigger_close,
                        confirm_bar=bar_index,
                        confirm_close=close,
                    ))
                    self._reset()

            if self._window_open and bar_index - self._trigger_bar >= self._pa_window:
                events.append(BreakoutEvent(
                    kind="failed",
                    is_up=self._is_up,
                    trigger_bar=self._trigger_bar,
                    trigger_close=self._trigger_close,
                ))
                self._reset()

        else:
            if is_strong and direction in ("bull", "bear"):
                is_up = direction == "bull"
                self._trigger_bar   = bar_index
                self._trigger_close = close
                self._is_up         = is_up
                self._window_open   = True
                events.append(BreakoutEvent(
                    kind="triggered",
                    is_up=is_up,
                    trigger_bar=bar_index,
                    trigger_close=close,
                ))

        return events

    @property
    def window_open(self) -> bool:
        return self._window_open

    @property
    def trigger_close(self) -> Optional[float]:
        return self._trigger_close if self._window_open else None

    @property
    def is_up(self) -> Optional[bool]:
        return self._is_up if self._window_open else None

    def reset(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._window_open:   bool  = False
        self._trigger_bar:   int   = 0
        self._trigger_close: float = 0.0
        self._is_up:         bool  = True
