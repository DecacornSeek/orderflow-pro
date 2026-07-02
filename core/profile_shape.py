"""Profile Shape Classification — determines market structure from VAP data.

Input: VAP dict (price_bucket -> volume), regardless of source (session, week, or 60-min).
Output: Shape classification with supporting metadata.

Shape definitions:
  P-Shape (Peak above, tails below)      -> Short covering / acceptance above
  b-Shape (Peak below, tails above)      -> Distribution / acceptance below
  D / Normal (single symmetric peak)     -> Balance, single acceptance area
  B-Shape (two significant peaks)        -> Double distribution (e.g. post-news)

Algorithm:
  1. Smooth histogram with simple moving average
  2. Find local maxima (peaks) above threshold
  3. Count significant peaks (>= X% of max volume)
  4. Determine shape from peak positions relative to total range

Alle Schwellwerte kommen aus ProfileConfig.
"""

from typing import Any, Dict, List, Optional, Tuple

from core.profile_config import ProfileConfig, PROFILE_CONFIG


def smooth_vap(vap: Dict[int, float], window: Optional[int] = None) -> Dict[int, float]:
    """Apply simple moving average to VAP data.

    Args:
        vap: price_bucket -> volume mapping.
        window: SMA window size (odd number recommended). Defaults to config.

    Returns:
        New dict with smoothed volumes (same keys).
    """
    if window is None:
        window = PROFILE_CONFIG.smoothing_window
    if not vap:
        return {}

    buckets = sorted(vap.keys())
    volumes = [vap[b] for b in buckets]

    smoothed = list(volumes)
    half = window // 2
    for i in range(len(volumes)):
        lo = max(0, i - half)
        hi = min(len(volumes), i + half + 1)
        smoothed[i] = sum(volumes[lo:hi]) / (hi - lo)

    return {b: round(v, 4) for b, v in zip(buckets, smoothed)}


def find_peaks(smoothed: Dict[int, float]) -> List[Tuple[int, float]]:
    """Find local maxima in smoothed VAP data.

    A bucket is a local maximum if its volume is >= both neighbours'
    volumes (and not zero). Edges are only compared against their single
    neighbour.

    Returns:
        List of (price_bucket, smoothed_volume) for each peak, sorted by volume descending.
    """
    if not smoothed:
        return []

    buckets = sorted(smoothed.keys())
    volumes = [smoothed[b] for b in buckets]
    peaks: List[Tuple[int, float]] = []

    for i in range(len(buckets)):
        vol = volumes[i]
        if vol <= 0:
            continue
        left_ok = (i == 0) or (vol >= volumes[i - 1])
        right_ok = (i == len(buckets) - 1) or (vol >= volumes[i + 1])
        if left_ok and right_ok:
            peaks.append((buckets[i], vol))

    # Sort by volume descending
    peaks.sort(key=lambda x: x[1], reverse=True)
    return peaks


def classify_shape(vap: Optional[Dict[int, float]],
                   config: Optional[ProfileConfig] = None,
                   significant_pct: Optional[float] = None) -> Dict[str, Any]:
    """Classify the shape of a VAP profile.

    Args:
        vap: price_bucket -> volume mapping. None or empty returns "unknown".
        config: ProfileConfig for thresholds. Defaults to PROFILE_CONFIG.
        significant_pct: Override for peak significance fraction.
            Defaults to config.significant_peak_pct.

    Returns:
        dict with keys:
            shape: str ("P", "b", "D", "B", "unknown")
            peak_count: int (total peaks found)
            significant_peaks: int (peaks >= significant_pct of max)
            top_peak_bucket: int or None (bucket with highest volume = POC)
            top_peak_volume: float
            secondary_peak_bucket: int or None (if B-shape)
            smoothed_vap: Dict[int, float] (smoothed values)
    """
    cfg = config or PROFILE_CONFIG
    if significant_pct is None:
        significant_pct = cfg.significant_peak_pct

    result: Dict[str, Any] = {
        "shape": "unknown",
        "peak_count": 0,
        "significant_peaks": 0,
        "top_peak_bucket": None,
        "top_peak_volume": 0.0,
        "secondary_peak_bucket": None,
        "smoothed_vap": {},
    }

    if not vap:
        return result

    smoothed = smooth_vap(vap, window=cfg.smoothing_window)
    result["smoothed_vap"] = smoothed

    peaks = find_peaks(smoothed)
    result["peak_count"] = len(peaks)

    if not peaks:
        result["shape"] = "unknown"
        return result

    max_vol = peaks[0][1]
    significant = [p for p in peaks if p[1] >= significant_pct * max_vol]
    result["significant_peaks"] = len(significant)
    result["top_peak_bucket"] = significant[0][0]
    result["top_peak_volume"] = significant[0][1]

    if len(significant) == 0:
        result["shape"] = "unknown"
    elif len(significant) >= 2:
        result["shape"] = "B"
        result["secondary_peak_bucket"] = significant[1][0]
    else:
        # One significant peak: determine P vs b vs D by position in range
        top_bucket = significant[0][0]
        buckets = sorted(vap.keys())
        if len(buckets) < 3:
            result["shape"] = "D"  # too few buckets for meaningful shape
        else:
            min_b = buckets[0]
            max_b = buckets[-1]
            total_range = max_b - min_b
            if total_range == 0:
                result["shape"] = "D"
            else:
                # Normalised position of peak: 0.0 = bottom, 1.0 = top
                pos = (top_bucket - min_b) / total_range
                if pos < cfg.peak_position_lower:
                    result["shape"] = "b"
                elif pos > cfg.peak_position_upper:
                    result["shape"] = "P"
                else:
                    result["shape"] = "D"

    return result
