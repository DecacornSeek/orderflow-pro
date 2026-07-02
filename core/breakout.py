"""
NCI Breakout Evaluator — Python port of nci_range_breakout.pine.

SOURCE OF TRUTH
---------------
trading/nci_range_breakout.pine (© NCI System — Range & Distribution)
All thresholds, condition names, and priority order mirror the Pine Script exactly.
Do NOT adjust without also updating the Pine reference script.

OVERVIEW
--------
When a bar closes outside an established range boundary, the system evaluates
three breakout standards in priority order (first match wins):

  Standard A – Two consecutive MaOrSM candles, same direction, both close outside
               boundary.  Check two conditions:
               condA: current close extends further than previous (progressive)
               condB: current totalLen >= 70% of previous (maintains size)
               → Both met  ⇒  "confirmed"
               → One fails ⇒  "pending" (enters PA window)

  Standard B – Single large MaOrSM[1] with ≥30% of its body past boundary, plus
               a small follow candle[0].  Check three conditions:
               condA: tl[1] > maxMaru5 (previous bar is the biggest recent candle)
               condB: tl[0] <= 30% of tl[1]  (follow is genuinely small)
               condC: follow bar does not re-enter the lower/upper half of big bar
               → All three  ⇒  "confirmed"
               → One fails  ⇒  "pending"

  Standard C – Single MaOrSM[0] with ≥30% of body past boundary and size ≥70%
               of maxMaru5.  Always "pending" — never immediate.

PA WINDOW (Price Action confirmation)
--------------------------------------
After any "pending" outcome, the system tracks the next paWindow bars (default=4).
A confirming bar must:
  • Close on the correct side of bo_line  (close > bo_line  for up)
  • Close beyond the combined candle extreme  (close > ext_high for up)
  • NOT be a Doji  (f_isValidConf in Pine = any type except Doji)
If no confirmation arrives within the window the attempt is voided.

SIZE GUARD — maxMaru5
----------------------
maxMaru5 = max total length of last 5 Marubozu / ShockMove candles.
Used by Standards A and B.  When None (buffer empty), the size guard is skipped.
The fallback inside Standard B mirrors Pine's:
  _mx = maxMaru5 if not None else (tl1 if tl1 > 0 else tl0)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from core.candle_classifier import DOJI

# ─────────────────────────────────────────────────────────────────────────────
# Thresholds — from nci_range_breakout.pine
# ─────────────────────────────────────────────────────────────────────────────
_STD_A_SIZE_PCT      = 0.60   # both candles must be >= 60% of maxMaru5
_STD_A_SIZE_RATIO    = 0.70   # condB: current totalLen >= 70% of previous
_STD_B_BODY_PAST_PCT = 0.30   # condD: body past boundary >= 30% of body
_STD_B_SMALL_PCT     = 0.30   # condB: follow candle totalLen <= 30% of big
_STD_B_MIDPOINT_PCT  = 0.50   # condC: follow not past midpoint of big candle
_STD_C_BODY_PAST_PCT = 0.30   # 30% of body past boundary
_STD_C_SIZE_PCT      = 0.70   # totalLen >= 70% of maxMaru5
PA_WINDOW_DEFAULT    = 4      # default confirmation window length (bars)


# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BreakoutOutcome:
    """Result of evaluating one breakout attempt.

    outcome  : "confirmed" — immediate signal, no PA window needed.
               "pending"   — enters PA window, needs confirmation.
               "none"      — no breakout standard was triggered.
    standard : "A" | "B" | "C" | ""  (empty when outcome is "none")
    is_up    : True for upward breakout, False for downward.
    bo_line  : The range boundary that was broken (used to anchor PA window).
    ext_high : Highest close/high of the breakout candle(s) — PA window upper bar.
    ext_low  : Lowest close/low of the breakout candle(s) — PA window lower bar.
    """
    outcome:  Literal["confirmed", "pending", "none"]
    standard: Literal["A", "B", "C", ""]
    is_up:    bool
    bo_line:  float
    ext_high: float
    ext_low:  float


@dataclass
class PAWindow:
    """Tracks an open PA confirmation window (mutable state).

    Created when a breakout returns "pending".
    Call ``check(candle_type, close, bar_offset)`` on each subsequent bar.

    Attributes
    ----------
    bo_line   : The boundary price that was broken.
    ext_high  : Upper extreme of the pending breakout candles.
    ext_low   : Lower extreme of the pending breakout candles.
    is_up     : Breakout direction.
    opened_at : Bar index when the pending signal was created (for offset tracking).
    window    : Maximum bars to wait for confirmation.
    confirmed : Set to True when a valid confirming bar is seen.
    expired   : Set to True when window runs out without confirmation.
    """
    bo_line:   float
    ext_high:  float
    ext_low:   float
    is_up:     bool
    opened_at: int = 0
    window:    int = PA_WINDOW_DEFAULT
    confirmed: bool = field(default=False, init=False)
    expired:   bool = field(default=False, init=False)

    def check(
        self,
        candle_type: str,
        close: float,
        bar_offset: int,
    ) -> bool:
        """Evaluate one bar against the open PA window.

        Parameters
        ----------
        candle_type : CandleClassifier result (e.g. MARUBOZU, NORMAL, DOJI …)
        close       : Close price of the bar.
        bar_offset  : How many bars since the window opened (1 = first bar after).

        Returns True if this bar confirms the breakout; also sets self.confirmed.
        Marks self.expired if bar_offset > window without confirmation.
        """
        if self.confirmed or self.expired:
            return False

        if bar_offset > self.window:
            self.expired = True
            return False

        # Pine: f_isValidConf = f_isMaOrSM or (not Maru and not SM and not Doji)
        #      = anything except Doji
        is_valid_conf = candle_type != DOJI

        if self.is_up:
            hit = close > self.bo_line and close > self.ext_high and is_valid_conf
        else:
            hit = close < self.bo_line and close < self.ext_low and is_valid_conf

        if hit:
            self.confirmed = True
        return hit


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _body_past_boundary(
    open_: float, close: float, boundary: float, is_up: bool
) -> float:
    """Amount of body that lies beyond the boundary (always >= 0).

    Pine (up):   max(close - max(open, bnd), 0)
    Pine (down): max(min(open, bnd) - close,  0)
    """
    if is_up:
        return max(close - max(open_, boundary), 0.0)
    else:
        return max(min(open_, boundary) - close, 0.0)


def _effective_max(
    max_maru5: Optional[float],
    tl1: float,
    tl0: float,
) -> Optional[float]:
    """Pine: _mx = not na(maxMaru5) ? maxMaru5 : (_tl1 > 0 ? _tl1 : _tl0)"""
    if max_maru5 is not None:
        return max_maru5
    if tl1 > 0:
        return tl1
    if tl0 > 0:
        return tl0
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Standard A — Two consecutive MaOrSM, same direction, both close outside
# ─────────────────────────────────────────────────────────────────────────────

def _std_a(
    c0_open: float, c0_high: float, c0_low: float, c0_close: float,
    c0_is_strong: bool,
    c1_open: float, c1_high: float, c1_low: float, c1_close: float,
    c1_is_strong: bool,
    boundary: float,
    is_up: bool,
    max_maru5: Optional[float],
) -> Literal["ok", "pending", "skip"]:
    """
    Pine Standard A conditions:
      • Both c0 and c1 must be MaOrSM
      • Both totalLen >= 60% of maxMaru5  (_aSize)
      • Both close outside boundary, same bull/bear direction  (_sameDir, _prevOut)
      condA: c0 close extends further than c1 (progressive)
      condB: c0 totalLen >= 70% of c1 totalLen  (size not deteriorating)
      f=0 → "ok",  f=1 → "pending",  f>=2 → "skip"
    """
    if not (c0_is_strong and c1_is_strong):
        return "skip"

    tl0 = c0_high - c0_low
    tl1 = c1_high - c1_low
    mx  = _effective_max(max_maru5, tl1, tl0)

    _a_size = (mx is None) or (tl0 >= _STD_A_SIZE_PCT * mx and tl1 >= _STD_A_SIZE_PCT * mx)

    if is_up:
        same_dir = c0_close > c0_open and c1_close > c1_open
        prev_out = c1_close > boundary
    else:
        same_dir = c0_close < c0_open and c1_close < c1_open
        prev_out = c1_close < boundary

    if not (_a_size and same_dir and prev_out):
        return "skip"

    cond_a = (c0_close > c1_close) if is_up else (c0_close < c1_close)
    cond_b = (tl1 > 0) and (tl0 >= _STD_A_SIZE_RATIO * tl1)
    fails  = (0 if cond_a else 1) + (0 if cond_b else 1)

    if fails == 0:
        return "ok"
    if fails == 1:
        return "pending"
    return "skip"


# ─────────────────────────────────────────────────────────────────────────────
# Standard B — Large MaOrSM[1] with 30%+ body past boundary + small follow[0]
# ─────────────────────────────────────────────────────────────────────────────

def _std_b(
    c0_open: float, c0_high: float, c0_low: float, c0_close: float,
    c1_open: float, c1_high: float, c1_low: float, c1_close: float,
    c1_is_strong: bool,
    boundary: float,
    is_up: bool,
    max_maru5: Optional[float],
) -> Literal["ok", "pending", "skip"]:
    """
    Pine Standard B conditions:
      • c1 must be MaOrSM, correct direction, close outside boundary
      • c1 body past boundary >= 30% of c1 body  (_condD)
      condA: c1 totalLen > maxMaru5  (c1 is the biggest candle in recent memory)
      condB: c0 totalLen <= 30% of c1 totalLen  (follow is small)
      condC: follow bar does not re-enter lower/upper half of big bar
      f=0 → "ok",  f=1 → "pending",  f>=2 → "skip"
    """
    if not c1_is_strong:
        return "skip"

    tl0 = c0_high - c0_low
    tl1 = c1_high - c1_low
    b1  = abs(c1_close - c1_open)
    mx  = _effective_max(max_maru5, tl1, tl0)

    if is_up:
        c1_dir = c1_close > c1_open
        c1_out = c1_close > boundary
    else:
        c1_dir = c1_close < c1_open
        c1_out = c1_close < boundary

    b_past = _body_past_boundary(c1_open, c1_close, boundary, is_up)
    cond_d = (b1 > 0) and (b_past / b1 >= _STD_B_BODY_PAST_PCT)

    if not (c1_dir and c1_out and cond_d):
        return "skip"

    cond_a = (mx is not None) and (tl1 > mx)
    cond_b = (tl1 > 0) and (tl0 <= _STD_B_SMALL_PCT * tl1)
    if is_up:
        cond_c = c0_low > c1_low + _STD_B_MIDPOINT_PCT * tl1
    else:
        cond_c = c0_high < c1_high - _STD_B_MIDPOINT_PCT * tl1

    fails = (0 if cond_a else 1) + (0 if cond_b else 1) + (0 if cond_c else 1)

    if fails == 0:
        return "ok"
    if fails == 1:
        return "pending"
    return "skip"


# ─────────────────────────────────────────────────────────────────────────────
# Standard C — Single strong candle, 30%+ body past boundary, size >= 70% max
# ─────────────────────────────────────────────────────────────────────────────

def _std_c(
    c0_open: float, c0_high: float, c0_low: float, c0_close: float,
    c0_body: float,
    c0_is_strong: bool,
    boundary: float,
    is_up: bool,
    max_maru5: Optional[float],
) -> bool:
    """
    Pine Standard C — always pending, never confirmed.
      • c0 must be MaOrSM, correct direction
      • 30%+ of body past boundary  (_c30)
      • totalLen >= 70% of maxMaru5  (_cSize)
    Returns True if Standard C triggers (pending).
    """
    if not c0_is_strong:
        return False

    tl0 = c0_high - c0_low
    c0_dir = (c0_close > c0_open) if is_up else (c0_close < c0_open)
    c_past = _body_past_boundary(c0_open, c0_close, boundary, is_up)
    c30    = (c0_body > 0) and (c_past / c0_body >= _STD_C_BODY_PAST_PCT)
    c_size = (max_maru5 is None) or (tl0 >= _STD_C_SIZE_PCT * max_maru5)

    return c0_dir and c30 and c_size


# ─────────────────────────────────────────────────────────────────────────────
# Public API — evaluate_breakout
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_breakout(
    # Current bar [0]
    c0_open:      float,
    c0_high:      float,
    c0_low:       float,
    c0_close:     float,
    c0_is_strong: bool,   # is_marubozu or is_shock_move
    # Previous bar [1]
    c1_open:      float,
    c1_high:      float,
    c1_low:       float,
    c1_close:     float,
    c1_is_strong: bool,
    # Context
    boundary:     float,           # range boundary being broken (_preTop or _preBot)
    is_up:        bool,            # True = upward breakout
    max_maru5:    Optional[float], # from CandleClassifier.max_maru5
) -> BreakoutOutcome:
    """Evaluate one breakout event against Standards A → B → C (priority order).

    Call this exactly once per bar when ``close > boundary`` (up) or
    ``close < boundary`` (down).  Returns a :class:`BreakoutOutcome` whose
    ``outcome`` field is ``"confirmed"``, ``"pending"``, or ``"none"``.

    Parameters mirror the Pine Script's distribution-check block verbatim.
    c0 = bar_index[0] (current, the one that just triggered the check),
    c1 = bar_index[1] (previous).
    """
    c0_body = abs(c0_close - c0_open)

    # ext_high / ext_low: the combined extreme of the triggering candle(s).
    # Pine: boExtHigh = _cPnd ? high[0] : max(high[1], high[0])
    #        boExtLow  = _cPnd ? low[0]  : min(low[1],  low[0])
    # We compute both eagerly; the caller can pick the right pair.
    combined_ext_high = max(c0_high, c1_high)
    combined_ext_low  = min(c0_low,  c1_low)

    # ── Standard A ────────────────────────────────────────────────────────────
    a = _std_a(
        c0_open, c0_high, c0_low, c0_close, c0_is_strong,
        c1_open, c1_high, c1_low, c1_close, c1_is_strong,
        boundary, is_up, max_maru5,
    )
    if a == "ok":
        return BreakoutOutcome("confirmed", "A", is_up, boundary, combined_ext_high, combined_ext_low)
    if a == "pending":
        return BreakoutOutcome("pending",   "A", is_up, boundary, combined_ext_high, combined_ext_low)

    # ── Standard B ────────────────────────────────────────────────────────────
    b = _std_b(
        c0_open, c0_high, c0_low, c0_close,
        c1_open, c1_high, c1_low, c1_close, c1_is_strong,
        boundary, is_up, max_maru5,
    )
    if b == "ok":
        return BreakoutOutcome("confirmed", "B", is_up, boundary, combined_ext_high, combined_ext_low)
    if b == "pending":
        return BreakoutOutcome("pending",   "B", is_up, boundary, combined_ext_high, combined_ext_low)

    # ── Standard C ────────────────────────────────────────────────────────────
    if _std_c(c0_open, c0_high, c0_low, c0_close, c0_body, c0_is_strong,
              boundary, is_up, max_maru5):
        # Standard C always pending; ext uses only c0 (Pine: boExtHigh = high[0])
        return BreakoutOutcome("pending", "C", is_up, boundary, c0_high, c0_low)

    return BreakoutOutcome("none", "", is_up, boundary, combined_ext_high, combined_ext_low)
