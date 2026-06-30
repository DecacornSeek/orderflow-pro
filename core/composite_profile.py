"""Composite Profile — overlays multiple archived profiles for structural context.

Purpose:
  Individual session/week profiles are noisy. Composite overlays multiple profiles
  of the same type (e.g. all NY sessions this week, or the last N weekly profiles)
  to extract stable range boundaries (HVN, LVN) and Balance/Imbalance state.

Output:
  - Composite POC and Value Area
  - List of HVN (High Volume Node) zones with strength
  - List of LVN (Low Volume Node) zones with strength
  - Balance/Imbalance flag based on Value Area overlap across consecutive profiles
"""

from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from core.session_profile import ProfileSnapshot, _compute_poc, _compute_value_area

HVN_VOLUME_PCT = 0.05   # a bucket with >= 5% of total composite volume is an HVN
LVN_MAX_VOLUME = 0.01    # a bucket with <= 1% of total composite volume is an LVN
PROFILE_HISTORY_LIMIT = 20  # max profiles kept in ring buffer


def average_vap(profiles: List[ProfileSnapshot]) -> Dict[int, float]:
    """Sum VAP data across multiple profiles, bucketed by price.

    Returns a dict of price_bucket -> summed volume.
    """
    composite: Dict[int, float] = {}
    for p in profiles:
        for bucket, vol in p.vap.items():
            composite[bucket] = composite.get(bucket, 0.0) + vol
    return composite


def find_hvn_zones(composite_vap: Dict[int, float],
                   threshold_pct: float = HVN_VOLUME_PCT) -> List[Dict[str, Any]]:
    """Find High Volume Node zones — contiguous buckets with volume above threshold.

    Args:
        composite_vap: price_bucket -> summed volume.
        threshold_pct: Fraction of max volume that defines "high volume".

    Returns:
        List of zones: {price_low, price_high, volume, peak_volume}.
    """
    if not composite_vap:
        return []

    max_vol = max(composite_vap.values()) if composite_vap else 1.0
    threshold = max_vol * threshold_pct

    buckets = sorted(composite_vap.keys())
    zones: List[Dict[str, Any]] = []
    current_zone: Optional[Dict] = None

    for b in buckets:
        vol = composite_vap[b]
        if vol >= threshold:
            if current_zone is None:
                current_zone = {"price_low": b, "price_high": b, "volume": vol, "peak_volume": vol}
            else:
                current_zone["price_high"] = b
                current_zone["volume"] += vol
                if vol > current_zone["peak_volume"]:
                    current_zone["peak_volume"] = vol
        else:
            if current_zone is not None:
                zones.append(current_zone)
                current_zone = None

    if current_zone is not None:
        zones.append(current_zone)

    return zones


def find_lvn_zones(composite_vap: Dict[int, float],
                   max_vol_pct: float = LVN_MAX_VOLUME) -> List[Dict[str, Any]]:
    """Find Low Volume Node zones — contiguous buckets with volume below threshold.

    Args:
        composite_vap: price_bucket -> summed volume.
        max_vol_pct: Fraction of max volume that defines "low volume".

    Returns:
        List of zones: {price_low, price_high, volume, trough_volume}.
    """
    if not composite_vap:
        return []

    max_vol = max(composite_vap.values()) if composite_vap else 1.0
    threshold = max_vol * max_vol_pct

    buckets = sorted(composite_vap.keys())
    zones: List[Dict[str, Any]] = []
    current_zone: Optional[Dict] = None

    for b in buckets:
        vol = composite_vap[b]
        if vol <= threshold:
            if current_zone is None:
                current_zone = {"price_low": b, "price_high": b, "volume": vol, "trough_volume": vol}
            else:
                current_zone["price_high"] = b
                current_zone["volume"] += vol
                if vol < current_zone["trough_volume"]:
                    current_zone["trough_volume"] = vol
        else:
            if current_zone is not None:
                zones.append(current_zone)
                current_zone = None

    if current_zone is not None:
        zones.append(current_zone)

    return zones


def check_value_area_overlap(profile_a: ProfileSnapshot,
                              profile_b: ProfileSnapshot) -> Dict[str, Any]:
    """Compare two profiles to determine Balance vs Imbalance.

    If Value Areas overlap -> Balance (range not expanding).
    If Value Areas drift apart -> Imbalance (range shifting/expanding).

    Returns:
        dict: {overlap, overlap_pct, direction}
    """
    if None in (profile_a.value_area_low, profile_a.value_area_high,
                profile_b.value_area_low, profile_b.value_area_high):
        return {"overlap": False, "overlap_pct": 0.0, "direction": "unknown"}

    # Overlap range
    ol_low = max(profile_a.value_area_low, profile_b.value_area_low)
    ol_high = min(profile_a.value_area_high, profile_b.value_area_high)

    overlap = ol_low < ol_high
    if overlap:
        range_a = profile_a.value_area_high - profile_a.value_area_low
        range_b = profile_b.value_area_high - profile_b.value_area_low
        overlap_amount = ol_high - ol_low
        overlap_pct = (2 * overlap_amount) / (range_a + range_b) if (range_a + range_b) > 0 else 0.0
    else:
        overlap_pct = 0.0

    # Direction: which way is the VA drifting?
    a_mid = (profile_a.value_area_high + profile_a.value_area_low) / 2.0
    b_mid = (profile_b.value_area_high + profile_b.value_area_low) / 2.0
    direction = "up" if b_mid > a_mid else ("down" if b_mid < a_mid else "flat")

    return {
        "overlap": overlap,
        "overlap_pct": round(overlap_pct, 4),
        "direction": direction,
    }


class CompositeProfile:
    """Composite profile builder — overlays multiple archived profiles.

    Usage:
        comp = CompositeProfile()
        comp.add_profiles(session_profile.get_archived_profiles()[:10])
        ctx = comp.current_context()  # dict with HVN, LVN, balance/imbalance
    """

    def __init__(self, history_limit: int = PROFILE_HISTORY_LIMIT) -> None:
        self._profiles: List[ProfileSnapshot] = []
        self._history_limit = history_limit

    def add_profiles(self, profiles: List[ProfileSnapshot]) -> None:
        """Add archived profiles to the composite ring buffer."""
        self._profiles.extend(profiles)
        if len(self._profiles) > self._history_limit:
            self._profiles = self._profiles[-self._history_limit:]

    def clear(self) -> None:
        """Clear all profiles."""
        self._profiles = []

    def current_context(self) -> Dict[str, Any]:
        """Compute and return composite profile context.

        Returns:
            dict with keys:
              - composite_poc: int or None
              - composite_value_area_high: int or None
              - composite_value_area_low: int or None
              - composite_total_volume: float
              - hvn_zones: list of HVN zone dicts
              - lvn_zones: list of LVN zone dicts
              - balance_flag: str ("balance", "imbalance_up", "imbalance_down", "insufficient_data")
              - profile_count: int (how many profiles in composite)
        """
        ctx: Dict[str, Any] = {
            "composite_poc": None,
            "composite_value_area_high": None,
            "composite_value_area_low": None,
            "composite_total_volume": 0.0,
            "hvn_zones": [],
            "lvn_zones": [],
            "balance_flag": "insufficient_data",
            "profile_count": len(self._profiles),
        }

        if len(self._profiles) < 2:
            return ctx

        composite_vap = average_vap(self._profiles)
        if not composite_vap:
            return ctx

        ctx["composite_total_volume"] = round(sum(composite_vap.values()), 4)

        # Composite POC
        poc = _compute_poc(composite_vap)
        ctx["composite_poc"] = poc

        # Composite Value Area
        va_h, va_l = _compute_value_area(composite_vap, target_pct=0.7)
        ctx["composite_value_area_high"] = va_h
        ctx["composite_value_area_low"] = va_l

        # HVN / LVN zones
        ctx["hvn_zones"] = find_hvn_zones(composite_vap)
        ctx["lvn_zones"] = find_lvn_zones(composite_vap)

        # Balance / Imbalance: compare last two profiles
        if len(self._profiles) >= 2:
            last = self._profiles[-1]
            prev = self._profiles[-2]
            overlap_info = check_value_area_overlap(prev, last)
            if overlap_info["overlap"]:
                ctx["balance_flag"] = "balance"
            else:
                direction = overlap_info["direction"]
                ctx["balance_flag"] = f"imbalance_{direction}"

        return ctx
