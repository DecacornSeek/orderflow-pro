"""Profile Configuration — single source of truth for all tunable thresholds.

Every magic number that was scattered across the profile stack lives here.
Modules accept an optional ProfileConfig instance; when None, PROFILE_CONFIG
(the default instance) is used. This keeps call sites clean while ensuring
every threshold can be overridden per-asset, per-timeframe, or per-strategy
without touching module internals.

Principles:
  - No module-level magic numbers anywhere in the profile stack.
  - Every threshold has a documented meaning and a sensible default.
  - Config is a frozen dataclass — immutable after creation, hashable for caching.
"""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class ProfileConfig:
    """All tunable profile parameters in one place.

    Create once at application startup, pass to all profile constructors.
    For most use cases the default instance (PROFILE_CONFIG) is sufficient.
    """

    # -- VAP / Binning -------------------------------------------------------
    bucket_size: int = 25
    """Price bucket granularity in dollars. 25 = $25 buckets (BTC default).
    Use 10 for ETH, 50 for weekly BTC, etc."""

    # -- Value Area ----------------------------------------------------------
    value_area_pct: float = 0.70
    """Fraction of total volume defining the Value Area (default 70%)."""

    # -- Regime --------------------------------------------------------------
    regime_balanced_ratio: float = 0.50
    """VA width / total price range >= this → 'balanced', else 'imbalanced'."""

    # -- POC Drift -----------------------------------------------------------
    poc_drift_interval_s: float = 60.0
    """How often to snapshot POC for drift tracking (seconds). 300 for weekly."""

    # -- Archive -------------------------------------------------------------
    archive_maxlen: int = 60
    """Max archived profiles in ring buffer. 52 for weekly."""

    # -- Initial Balance -----------------------------------------------------
    initial_balance_minutes: int = 60
    """Minutes of trading defining the Initial Balance window."""

    # -- Pre-Session Anomaly -------------------------------------------------
    anomaly_factor_threshold: float = 1.50
    """Volume multiplier over baseline to flag as anomalous."""

    pre_session_baseline_path: Optional[Path] = None
    """Path to pre-session baseline JSON. If None, defaults to
    <project_root>/data/pre_session_baseline.json."""

    # -- Smoothing & Peak Detection ------------------------------------------
    smoothing_window: int = 3
    """SMA window size for VAP smoothing (odd recommended)."""

    significant_peak_pct: float = 0.60
    """Peak must be ≥ this fraction of max volume to be 'significant'."""

    peak_position_lower: float = 0.33
    """Normalised position below which a peak is 'b' (bottom-accepting)."""

    peak_position_upper: float = 0.67
    """Normalised position above which a peak is 'P' (top-accepting)."""

    # -- Single Prints -------------------------------------------------------
    single_print_max_pct: float = 0.05
    """Bucket volume ≤ this fraction of POC volume → candidate single print."""

    single_print_min_run: int = 1
    """Minimum consecutive qualifying buckets to form a single-print zone."""

    # -- Extreme Classification ----------------------------------------------
    extreme_edge_buckets: int = 2
    """Number of edge buckets forming 'the extreme' at each end."""

    strong_extreme_max_ratio: float = 0.25
    """Edge / body avg volume ≤ this → 'strong' extreme (thin tail)."""

    weak_extreme_min_ratio: float = 0.75
    """Edge / body avg volume ≥ this → 'weak' extreme (poor high/low)."""

    min_buckets_for_extremes: int = 5
    """Profiles with fewer buckets get 'unknown' extreme classification."""

    # -- Composite / HVN / LVN -----------------------------------------------
    hvn_volume_pct: float = 0.05
    """Bucket with ≥ this fraction of composite max volume → HVN."""

    lvn_max_volume: float = 0.01
    """Bucket with ≤ this fraction of composite max volume → LVN."""

    profile_history_limit: int = 20
    """Max profiles kept in composite ring buffer."""

    # -- Business Zones ------------------------------------------------------
    merge_gap_buckets: int = 1
    """Zones of same kind with ≤ this many bucket gaps are merged."""

    max_zones: int = 12
    """Max zones returned in the snapshot payload."""


# Default instance — used when no explicit config is provided.
PROFILE_CONFIG = ProfileConfig()


def resolve_config(config: Optional[ProfileConfig], **overrides: Any) -> ProfileConfig:
    """Return config (or PROFILE_CONFIG) with any non-None overrides applied.

    Backward-compat helper: lets constructors keep accepting individual
    threshold kwargs (value_area_pct=0.7, initial_balance_minutes=60, ...)
    alongside the new config= parameter, so existing call sites don't have
    to build a ProfileConfig just to override one field. None values are
    treated as "not overridden" and skipped.
    """
    cfg = config or PROFILE_CONFIG
    active = {k: v for k, v in overrides.items() if v is not None}
    return replace(cfg, **active) if active else cfg
