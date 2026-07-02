"""
NCI Candle Classifier — Python port of nci_system_m1_candle_classifier.pine.

SOURCE OF TRUTH
---------------
trading/nci_system_m1_candle_classifier.pine (© NCI System — Module 1)
All thresholds and classification priority order mirror the Pine Script exactly.
Do NOT adjust thresholds without also updating the Pine reference script.

CLASSIFICATION PRIORITY ORDER (first match wins)
-------------------------------------------------
1. MARUBOZU    body >= 70% of total range (dominant body, almost no wicks)
2. SHOCK_MOVE  body >= 50% AND close within outermost 10% of range
               (extreme speed — "absorption zone")
3. DOJI        total range < 30% of avgMaru5 (tiny relative to recent strong bars)
4. PINBAR      body < 50% AND total range >= 30% avgMaru5 (wicky, not small)
5. NORMAL      anything else (including when benchmark not yet initialised)

BENCHMARK: avgMaru5
-------------------
Rolling average of the last 5 MARUBOZU + SHOCK_MOVE candles' total ranges.
Calibrates Doji/Pinbar thresholds to current volatility so "small" and "large"
are always meaningful relative to recent institutional commitment.
Until the buffer holds at least 1 entry, Doji and Pinbar cannot fire —
those bars are classified NORMAL, identical to Pine's `na(avgMaru5)` path.

DIRECTION
---------
Meaningful only for MARUBOZU and SHOCK_MOVE (colour is "noise" for others).
  "bull"   close > open
  "bear"   close < open
  "none"   open == close (flat body)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional


# ── Candle types (string constants, not enum, to stay JSON-serialisable) ──────
MARUBOZU   = "marubozu"
SHOCK_MOVE = "shock_move"
DOJI       = "doji"
PINBAR     = "pinbar"
NORMAL     = "normal"

CANDLE_TYPES = (MARUBOZU, SHOCK_MOVE, DOJI, PINBAR, NORMAL)

# ── Thresholds — directly from the Pine Script ────────────────────────────────
_MARU_BODY_RATIO   = 0.70   # body / totalLen >= 0.70  → Marubozu
_SM_BODY_RATIO     = 0.50   # body / totalLen >= 0.50  → ShockMove candidate
_SM_ABS_ZONE_PCT   = 0.10   # close within outermost 10% of range
_DOJI_RANGE_FACTOR = 0.30   # totalLen < 0.30 * avgMaru5  → Doji
_PB_BODY_RATIO     = 0.50   # body / totalLen < 0.50  → Pinbar candidate
_PB_MIN_FACTOR     = 0.30   # totalLen >= 0.30 * avgMaru5 (not too tiny)
_BENCHMARK_WINDOW  = 5      # number of strong candles in rolling benchmark


@dataclass(frozen=True)
class CandleResult:
    """Immutable result of a single candle classification."""
    candle_type:  str            # one of CANDLE_TYPES
    direction:    str            # "bull" | "bear" | "none"
    body_ratio:   float          # body / total_len  (0.0 if flat bar)
    total_len:    float          # high - low
    avg_maru5:    Optional[float] # current benchmark value (None if buffer empty)

    # Convenience properties
    @property
    def is_strong(self) -> bool:
        """True for MARUBOZU and SHOCK_MOVE — these feed the benchmark."""
        return self.candle_type in (MARUBOZU, SHOCK_MOVE)

    @property
    def is_indecision(self) -> bool:
        return self.candle_type in (DOJI, PINBAR)


class CandleClassifier:
    """Stateful NCI candle classifier with rolling avgMaru5 benchmark.

    Maintains the last-5-strong-candles buffer across successive calls.
    Feed bars in chronological order (oldest first, left to right).

    Usage:
        clf = CandleClassifier()
        result = clf.classify(open_=o, high=h, low=l, close=c)
    """

    def __init__(self) -> None:
        self._buf: deque[float] = deque(maxlen=_BENCHMARK_WINDOW)

    # ── Public API ────────────────────────────────────────────────────────────

    def classify(
        self,
        open_: float,
        high:  float,
        low:   float,
        close: float,
    ) -> CandleResult:
        """Classify one OHLC candle and update the internal benchmark.

        Parameters match standard OHLC naming; ``open_`` has a trailing
        underscore to avoid shadowing the Python built-in.
        """
        total_len  = high - low
        body       = abs(close - open_)
        body_ratio = body / total_len if total_len > 0 else 0.0

        avg_maru5: Optional[float] = (
            sum(self._buf) / len(self._buf) if self._buf else None
        )

        candle_type = self._classify(
            open_=open_, high=high, low=low, close=close,
            total_len=total_len, body_ratio=body_ratio,
            avg_maru5=avg_maru5,
        )

        # Update benchmark: MARUBOZU and SHOCK_MOVE feed the buffer.
        if candle_type in (MARUBOZU, SHOCK_MOVE):
            self._buf.append(total_len)

        direction = _direction(open_, close)

        return CandleResult(
            candle_type=candle_type,
            direction=direction,
            body_ratio=round(body_ratio, 4),
            total_len=total_len,
            avg_maru5=avg_maru5,
        )

    def reset(self) -> None:
        """Clear the benchmark buffer (use between sessions / test cases)."""
        self._buf.clear()

    @property
    def avg_maru5(self) -> Optional[float]:
        """Current benchmark value (None until first strong candle seen)."""
        return sum(self._buf) / len(self._buf) if self._buf else None

    @property
    def max_maru5(self) -> Optional[float]:
        """Maximum of last 5 strong candle total lengths.

        Used by breakout Standards A/B as the size benchmark.
        Mirrors Pine: ``float maxMaru5 = array.max(mBuf)``.
        Returns None when buffer is empty.
        """
        return max(self._buf) if self._buf else None

    # ── Classification logic (stateless given measurements) ───────────────────

    @staticmethod
    def _classify(
        open_: float, high: float, low: float, close: float,
        total_len: float, body_ratio: float,
        avg_maru5: Optional[float],
    ) -> str:
        # ── Priority 1: Marubozu ──────────────────────────────────────────────
        if total_len > 0 and body_ratio >= _MARU_BODY_RATIO:
            return MARUBOZU

        # ── Priority 2: Shock Move ────────────────────────────────────────────
        # body >= 50% AND close lands in absorption zone (outermost 10%)
        in_abs_zone = (
            close >= high - _SM_ABS_ZONE_PCT * total_len
            or close <= low  + _SM_ABS_ZONE_PCT * total_len
        )
        if total_len > 0 and body_ratio >= _SM_BODY_RATIO and in_abs_zone:
            return SHOCK_MOVE

        # ── Priority 3: Doji ──────────────────────────────────────────────────
        # Requires benchmark (avgMaru5 must exist)
        if avg_maru5 is not None and total_len < _DOJI_RANGE_FACTOR * avg_maru5:
            return DOJI

        # ── Priority 4: Pinbar ────────────────────────────────────────────────
        # body < 50% AND big enough not to be a Doji
        if (
            avg_maru5 is not None
            and body_ratio < _PB_BODY_RATIO
            and total_len >= _PB_MIN_FACTOR * avg_maru5
        ):
            return PINBAR

        # ── Priority 5: Normal (catch-all) ───────────────────────────────────
        return NORMAL


# ── Stateless helper used by tests and batch processing ──────────────────────

def classify_series(
    ohlc_rows: list[tuple[float, float, float, float]],
) -> list[CandleResult]:
    """Classify a sequence of (open, high, low, close) tuples in order.

    Equivalent to running the Pine Script on a chart bar-by-bar.
    """
    clf = CandleClassifier()
    return [clf.classify(o, h, l, c) for o, h, l, c in ohlc_rows]


def _direction(open_: float, close: float) -> str:
    if close > open_:
        return "bull"
    if close < open_:
        return "bear"
    return "none"
