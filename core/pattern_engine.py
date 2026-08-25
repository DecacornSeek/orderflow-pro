"""
Pattern Engine - deterministic volume significance + absorption detection.

Loads a weekly volume baseline from data/volume_baseline.parquet (built by
scripts/build_volume_baseline.py) and evaluates each incoming trade for
statistical significance using z-score against the baseline.

Maintains its own resettable session VAP (same BUCKET=25 logic as core/history.py
but a separate instance) for tracking intra-session volume per bucket.

If the baseline file is missing, the engine runs in degraded mode:
no z-score or absorption detection is performed (returns None for evaluate()),
but the engine does NOT crash - it logs a warning.

Usage:
    engine = PatternEngine()
    result = engine.evaluate(
        trade={"price": 50000.0, "size": 1.5, "side": "buy"},
        recent_cvd_snapshot={"rolling_delta": 0.3, "rolling_buy_volume": 5.0, ...}
    )
    # result -> dict | None
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import pandas as pd

BUCKET = 25
DEFAULT_Z_THRESHOLD = 2.0
DEFAULT_ABSORPTION_RATIO = 0.15
DEFAULT_MIN_WEEKS = 2  # minimum weeks of baseline data needed to compute a z-score

logger = logging.getLogger(__name__)

BASELINE_PATH = Path(__file__).parent.parent / "data" / "volume_baseline.parquet"


# ---------------------------------------------------------------------------
# Delta Divergenz (SYSTEM_ARCHITECTURE_V2.md §5.3)
# ---------------------------------------------------------------------------

def _divergence_strength(cvd_prev: float, cvd_last: float) -> float:
    """Normierte Staerke 0..1: wie weit die CVD hinter dem alten Extrem zurueckbleibt."""
    gap = abs(cvd_prev - cvd_last)
    denom = max(abs(cvd_prev), abs(cvd_last), 1e-9)
    return round(min(gap / denom, 1.0), 4)


def _spot_confirms(spot: Optional[Sequence[float]], bearish: bool) -> Optional[bool]:
    """Spot-CVD Bestaetigung: dreht Spot in dieselbe Richtung wie Perps?

    None = kein Spot-Stream verfuegbar (Sprint A pending) — Signal bleibt
    gueltig, nur ohne Bestaetigungs-Dimension.
    """
    if spot is None or len(spot) < 2:
        return None
    return spot[-1] <= spot[-2] if bearish else spot[-1] >= spot[-2]


def detect_delta_divergence(
    price_highs: Optional[Sequence[float]] = None,
    cvd_perps_highs: Optional[Sequence[float]] = None,
    cvd_spot_highs: Optional[Sequence[float]] = None,
    price_lows: Optional[Sequence[float]] = None,
    cvd_perps_lows: Optional[Sequence[float]] = None,
    cvd_spot_lows: Optional[Sequence[float]] = None,
) -> Optional[Dict[str, Any]]:
    """Delta Divergenz — Preis-Extrem ohne bestaetigendes CVD-Extrem.

    Reine Funktion, kein State (Architektur-Prinzip P3). Die Sequenzen sind
    parallele Serien bestaetigter Swing-Punkte (gleicher Swing = gleicher Index);
    verglichen werden jeweils die letzten beiden.

    Bearish: Preis macht Higher High, CVD Perps macht KEIN neues High
             -> Kaufdruck nimmt ab trotz steigendem Preis.
    Bullish: Preis macht Lower Low, CVD Perps macht KEIN neues Low
             -> Verkaufsdruck trocknet aus, moegliches Reversal.

    Spot-CVD (optional) liefert das Bestaetigungs-Flag "spot_confirms":
    True/False wenn Spot-Daten vorhanden, sonst None.

    Returns:
        dict mit {divergence_type, price, prev_price, cvd_perps,
        prev_cvd_perps, strength, spot_confirms} — oder None wenn
        keine Divergenz vorliegt. Feuern beide Seiten, gewinnt die staerkere.
    """
    bearish: Optional[Dict[str, Any]] = None
    bullish: Optional[Dict[str, Any]] = None

    if (price_highs is not None and cvd_perps_highs is not None
            and len(price_highs) >= 2 and len(cvd_perps_highs) >= 2):
        p_prev, p_last = price_highs[-2], price_highs[-1]
        c_prev, c_last = cvd_perps_highs[-2], cvd_perps_highs[-1]
        # Higher High im Preis, aber CVD bleibt unter/auf dem alten High
        if p_last > p_prev and c_last <= c_prev:
            bearish = {
                "divergence_type": "bearish_divergence",
                "price": p_last, "prev_price": p_prev,
                "cvd_perps": c_last, "prev_cvd_perps": c_prev,
                "strength": _divergence_strength(c_prev, c_last),
                "spot_confirms": _spot_confirms(cvd_spot_highs, bearish=True),
            }

    if (price_lows is not None and cvd_perps_lows is not None
            and len(price_lows) >= 2 and len(cvd_perps_lows) >= 2):
        p_prev, p_last = price_lows[-2], price_lows[-1]
        c_prev, c_last = cvd_perps_lows[-2], cvd_perps_lows[-1]
        # Lower Low im Preis, aber CVD bleibt ueber/auf dem alten Low
        if p_last < p_prev and c_last >= c_prev:
            bullish = {
                "divergence_type": "bullish_divergence",
                "price": p_last, "prev_price": p_prev,
                "cvd_perps": c_last, "prev_cvd_perps": c_prev,
                "strength": _divergence_strength(c_prev, c_last),
                "spot_confirms": _spot_confirms(cvd_spot_lows, bearish=False),
            }

    if bearish and bullish:
        return bearish if bearish["strength"] >= bullish["strength"] else bullish
    return bearish or bullish


class PatternEngine:
    """Deterministic pattern detection engine - no LLM involvement.

    Two modes:
        1. Baseline available - full z-score + absorption detection.
        2. Baseline missing - degraded mode (no scoring, no crash).

    Args:
        z_threshold: Minimum absolute z-score for significance (default 2.0).
        absorption_ratio: Max |delta|/volume ratio for absorption flag (default 0.15).
        min_weeks: Minimum weeks of baseline data required per bucket (default 2).
            Buckets with week_count < min_weeks are ineligible for significance.
    """

    def __init__(
        self,
        z_threshold: float = DEFAULT_Z_THRESHOLD,
        absorption_ratio: float = DEFAULT_ABSORPTION_RATIO,
        min_weeks: int = DEFAULT_MIN_WEEKS,
        baseline_path: Optional[Path] = None,
    ) -> None:
        self.z_threshold = z_threshold
        self.absorption_ratio = absorption_ratio
        self.min_weeks = min_weeks
        self._baseline_path = Path(baseline_path) if baseline_path else BASELINE_PATH

        # Session VAP (separate from core/history.py)
        self._session_vap: Dict[int, float] = {}

        # Baseline: converted to dict for O(1) lookup at load time
        self._baseline: Optional[Dict[int, Dict[str, float]]] = None
        self._load_baseline()

    # ------------------------------------------------------------------
    # Baseline loading
    # ------------------------------------------------------------------

    def _load_baseline(self) -> None:
        """Load volume baseline from parquet. Logs warning if missing.

        Converts to dict for O(1) lookup: {price_bucket: {mean, std, week_count}}
        """
        path = self._baseline_path
        if not path.exists():
            logger.warning(
                "PatternEngine: Volume baseline not found at %s. "
                "Running in degraded mode - no z-score or absorption detection. "
                "Run scripts/build_volume_baseline.py to create the baseline.",
                path,
            )
            return

        try:
            df = pd.read_parquet(path)
            self._baseline = {}
            for _, row in df.iterrows():
                bucket = int(row["price_bucket"])
                self._baseline[bucket] = {
                    "mean": float(row["mean_volume"]),
                    "std": float(row["std_volume"]),
                    "week_count": int(row["week_count"]),
                }
            logger.info(
                "PatternEngine: Loaded baseline with %d price buckets from %s.",
                len(self._baseline),
                path,
            )
        except Exception as e:
            logger.warning(
                "PatternEngine: Failed to load baseline from %s: %s. "
                "Running in degraded mode.",
                path,
                e,
            )
            self._baseline = None

    def reload_baseline(self, path: Optional[Path] = None) -> None:
        """Reload baseline from disk (e.g. after rebuilding)."""
        if path is not None:
            self._baseline_path = Path(path)
        self._load_baseline()

    @property
    def has_baseline(self) -> bool:
        """True if a valid baseline is loaded."""
        return self._baseline is not None and len(self._baseline) > 0

    # ------------------------------------------------------------------
    # Baseline diagnostics
    # ------------------------------------------------------------------

    def count_buckets_below_min_weeks(self) -> int:
        """Number of buckets with insufficient weeks for reliable statistics."""
        if not self._baseline:
            return 0
        return sum(
            1 for info in self._baseline.values()
            if info["week_count"] < self.min_weeks
        )

    def count_hits_on_weak_baseline(self, hits: list) -> int:
        """How many hits fall on buckets with week_count < min_weeks.

        Args:
            hits: List of result dicts from evaluate().

        Returns:
            Count of hits on weak-baseline buckets.
        """
        if not self._baseline:
            return 0
        n = 0
        for hit in hits:
            bucket = hit.get("price_bucket")
            info = self._baseline.get(bucket)
            if info and info["week_count"] < self.min_weeks:
                n += 1
        return n

    # ------------------------------------------------------------------
    # Session VAP
    # ------------------------------------------------------------------

    def reset_session_vap(self) -> None:
        """Reset intra-session volume accumulation (e.g. at session start)."""
        self._session_vap.clear()

    def _add_to_session_vap(self, price: float, size: float) -> None:
        """Accumulate volume into the session VAP bucket."""
        bucket = int(price / BUCKET) * BUCKET
        self._session_vap[bucket] = self._session_vap.get(bucket, 0.0) + size

    def get_session_vap(self) -> Dict[int, float]:
        """Return a copy of the current session VAP data."""
        return dict(self._session_vap)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        trade: Dict[str, Any],
        recent_cvd_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Evaluate a single trade for volume significance and absorption.

        Args:
            trade: dict with keys "price" (float), "size" (float), "side" (str).
            recent_cvd_snapshot: Optional dict from cvd.snapshot() containing
                rolling_delta, rolling_buy_volume, rolling_sell_volume, etc.
                If None, absorption detection is skipped.

        Returns:
            dict with keys {price_bucket, z_score, volume, absorption, timestamp}
            if the trade is statistically significant, otherwise None.
        """
        price = trade.get("price")
        size = trade.get("size")
        if price is None or size is None:
            return None

        # Always track session VAP
        self._add_to_session_vap(float(price), float(size))

        # Without baseline, no scoring possible
        if not self.has_baseline:
            return None

        bucket = int(float(price) / BUCKET) * BUCKET
        current_vol = self._session_vap.get(bucket, 0.0)

        # Lookup baseline stats for this bucket (O(1) dict lookup)
        info = self._baseline.get(bucket)  # type: ignore[union-attr]
        if info is None:
            return None  # No baseline data for this bucket

        mean_vol = info["mean"]
        std_vol = info["std"]
        week_count = info["week_count"]

        # Bucket must have enough weeks for a reliable baseline
        if week_count < self.min_weeks:
            return None

        # Division by zero guard (std = 0 when week_count < 2, but we already
        # checked min_weeks >= 2, making this a safety net only)
        if std_vol <= 0:
            return None

        z_score = (current_vol - mean_vol) / std_vol

        if abs(z_score) < self.z_threshold:
            return None  # Not significant

        # Absorption detection
        absorption = False
        if recent_cvd_snapshot is not None:
            rolling_delta = recent_cvd_snapshot.get("rolling_delta", 0.0)
            rolling_buy = recent_cvd_snapshot.get("rolling_buy_volume", 0.0)
            rolling_sell = recent_cvd_snapshot.get("rolling_sell_volume", 0.0)
            total_vol = rolling_buy + rolling_sell
            if total_vol > 0:
                abs_delta_ratio = abs(rolling_delta) / total_vol
                if abs_delta_ratio < self.absorption_ratio:
                    absorption = True

        return {
            "price_bucket": bucket,
            "z_score": round(z_score, 4),
            "volume": round(current_vol, 4),
            "absorption": absorption,
            "timestamp": trade.get("timestamp"),
        }
