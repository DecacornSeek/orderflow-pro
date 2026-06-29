"""
Pattern Engine - deterministic volume significance + absorption detection.

Loads a weekly volume baseline from data/volume_baseline.parquet (built by
scripts/build_volume_baseline.py) and evaluates each incoming trade for
statistical significance using z-score against the baseline.

Maintains its own resettable session VAP (same BUCKET=25 logic as core/history.py
but a separate instance) for tracking intra-session volume per bucket.

If the baseline file is missing, the engine runs in "Baseline fehlt" mode:
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
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

BUCKET = 25
DEFAULT_Z_THRESHOLD = 2.0
DEFAULT_ABSORPTION_RATIO = 0.15  # |rolling_delta| / total_window_volume < this => absorption

logger = logging.getLogger(__name__)

BASELINE_PATH = Path(__file__).parent.parent / "data" / "volume_baseline.parquet"


class PatternEngine:
    """Deterministic pattern detection engine - no LLM involvement.

    Two modes:
        1. Baseline available - full z-score + absorption detection.
        2. Baseline missing - degraded mode (no scoring, no crash).

    Args:
        z_threshold: Minimum absolute z-score for significance (default 2.0).
        absorption_ratio: Max |delta|/volume ratio for absorption flag (default 0.15).
    """

    def __init__(
        self,
        z_threshold: float = DEFAULT_Z_THRESHOLD,
        absorption_ratio: float = DEFAULT_ABSORPTION_RATIO,
    ) -> None:
        self.z_threshold = z_threshold
        self.absorption_ratio = absorption_ratio

        # Session VAP (separate from core/history.py)
        self._session_vap: Dict[int, float] = {}

        # Baseline
        self._baseline: Optional[pd.DataFrame] = None
        self._load_baseline()

    # ------------------------------------------------------------------
    # Baseline loading
    # ------------------------------------------------------------------

    def _load_baseline(self) -> None:
        """Load volume baseline from parquet. Logs warning if missing."""
        path = BASELINE_PATH
        if not path.exists():
            logger.warning(
                "PatternEngine: Volume baseline not found at %s. "
                "Running in degraded mode - no z-score or absorption detection. "
                "Run scripts/build_volume_baseline.py to create the baseline.",
                path,
            )
            return

        try:
            self._baseline = pd.read_parquet(path)
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

    def reload_baseline(self) -> None:
        """Reload baseline from disk (e.g. after rebuilding)."""
        self._load_baseline()

    @property
    def has_baseline(self) -> bool:
        """True if a valid baseline is loaded."""
        return self._baseline is not None and len(self._baseline) > 0

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

        # Lookup baseline stats for this bucket
        row = self._baseline[self._baseline["price_bucket"] == bucket]
        if row.empty:
            return None  # No baseline data for this bucket

        mean_vol = float(row.iloc[0]["mean_volume"])
        std_vol = float(row.iloc[0]["std_volume"])

        # z-score
        if std_vol <= 0:
            return None  # No variability, can't compute z-score
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
