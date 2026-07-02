"""Multi-Week VPOC Trend Series — global money flow aus Wochen-Profilen.

Methodology Step 5 (docs/METHODOLOGY_STEPS_5-8.md): "Trend stability — Trend
drawn only from weekly VPOC + value areas: is global money flow rising,
falling, flattening?"

Dieses Modul konsumiert archivierte WeeklyProfile-Snapshots, extrahiert die
VPOC-Serie und klassifiziert den multi-week Trend. Reine Funktionen (P3):
gleiche Profile -> gleiche Trend-Signatur (backtestbar).

Output-Signatur:
  - direction:    "rising" | "falling" | "flattening" | "insufficient_data"
  - strength:     0..1 (Konsistenz der Richtung ueber die Wochen)
  - slope:        $/Woche (lineare Regression ueber VPOC-Serie)
  - series:       [(label, poc, timestamp), ...] — die VPOC-Zeitreihe
  - weeks_analyzed: Anzahl der Wochen mit gueltigem POC
"""

from typing import Any, Dict, List, Optional, Tuple

from core.volume_profile import ProfileSnapshot


# ── Thresholds ───────────────────────────────────────────────────────────────
_MIN_WEEKS = 3            # minimum weeks with POC for a trend call
_FLAT_SLOPE_PCT = 0.002   # |slope| / avg_price < 0.2% → flattening
_TREND_CONSISTENCY = 0.60 # fraction of weeks moving in the dominant direction


def build_vpoc_series(
    profiles: List[ProfileSnapshot],
    require_poc: bool = True,
) -> List[Dict[str, Any]]:
    """Extract VPOC time series from archived weekly profiles.

    Args:
        profiles: List of ProfileSnapshot objects (typically from
                  weekly.get_archived_profiles()), sorted oldest-first.
        require_poc: If True, skip profiles without a POC.

    Returns:
        List of {label, poc, timestamp} dicts, sorted oldest-first.
    """
    series: List[Dict[str, Any]] = []
    for p in profiles:
        if require_poc and p.poc is None:
            continue
        if p.poc is None and not require_poc:
            continue
        series.append({
            "label": p.label,
            "poc": p.poc,
            "timestamp": p.timestamp,
        })
    return series


def classify_vpoc_trend(
    profiles: List[ProfileSnapshot],
    min_weeks: int = _MIN_WEEKS,
    flat_slope_pct: float = _FLAT_SLOPE_PCT,
    trend_consistency: float = _TREND_CONSISTENCY,
) -> Dict[str, Any]:
    """Classify the multi-week VPOC trend from archived weekly profiles.

    Pure function (P3): same profiles → same output.

    Algorithm:
      1. Extract VPOC series (oldest → newest).
      2. If < min_weeks: return "insufficient_data".
      3. Linear regression over (index, poc) → slope in $/week.
      4. If |slope| / avg_price < flat_slope_pct → "flattening".
      5. Count consecutive weeks moving in dominant direction:
         - If >= trend_consistency fraction → "rising" / "falling".
         - Else → "flattening" (choppy, no clear direction).

    Returns:
        {direction, strength, slope, slope_pct, avg_price, series,
         weeks_analyzed, direction_changes, consecutive_weeks,
         message}
    """
    series = build_vpoc_series(profiles)

    if len(series) < min_weeks:
        return {
            "direction": "insufficient_data",
            "strength": 0.0,
            "slope": 0.0,
            "slope_pct": 0.0,
            "avg_price": None,
            "series": series,
            "weeks_analyzed": len(series),
            "direction_changes": 0,
            "consecutive_weeks": 0,
            "message": f"need {min_weeks} weeks with POC, have {len(series)}",
        }

    pocs = [s["poc"] for s in series]
    n = len(pocs)
    avg_price = sum(pocs) / n

    # Linear regression: slope in $/week (x = index 0..n-1)
    x_mean = (n - 1) / 2.0
    y_mean = avg_price
    num = sum((i - x_mean) * (pocs[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    slope = num / den if den != 0 else 0.0
    slope_pct = slope / avg_price if avg_price > 0 else 0.0

    # Direction changes: count how often VPOC changes direction
    direction_changes = 0
    for i in range(1, n - 1):
        prev_diff = pocs[i] - pocs[i - 1]
        curr_diff = pocs[i + 1] - pocs[i]
        if (prev_diff > 0 and curr_diff < 0) or (prev_diff < 0 and curr_diff > 0):
            direction_changes += 1

    # Dominant direction: count weeks moving up vs down vs flat
    ups = sum(1 for i in range(1, n) if pocs[i] > pocs[i - 1])
    downs = sum(1 for i in range(1, n) if pocs[i] < pocs[i - 1])
    flats = n - 1 - ups - downs
    total_moves = n - 1

    if total_moves == 0:
        return {
            "direction": "insufficient_data",
            "strength": 0.0,
            "slope": round(slope, 2),
            "slope_pct": round(slope_pct, 6),
            "avg_price": round(avg_price, 2),
            "series": series,
            "weeks_analyzed": n,
            "direction_changes": direction_changes,
            "consecutive_weeks": 0,
            "message": "no directional moves to analyze",
        }

    # Flattening: slope near zero
    if abs(slope_pct) < flat_slope_pct:
        direction = "flattening"
        strength = round(1.0 - (direction_changes / max(total_moves, 1)), 4)
    else:
        dominant = "rising" if slope > 0 else "falling"
        dominant_count = ups if slope > 0 else downs
        consistency = dominant_count / total_moves

        if consistency >= trend_consistency:
            direction = dominant
        else:
            direction = "flattening"
        strength = round(consistency, 4)

    # Consecutive weeks in current direction (most recent)
    consecutive = 0
    if n >= 2:
        latest_dir = 1 if pocs[-1] > pocs[-2] else (-1 if pocs[-1] < pocs[-2] else 0)
        for i in range(n - 1, 0, -1):
            d = 1 if pocs[i] > pocs[i - 1] else (-1 if pocs[i] < pocs[i - 1] else 0)
            if d == latest_dir and latest_dir != 0:
                consecutive += 1
            else:
                break

    msg = (
        f"VPOC {direction} (strength={strength:.2f}, "
        f"slope=${slope:.1f}/wk, {slope_pct*100:+.2f}%, "
        f"{consecutive}wk consecutive, {n} weeks)"
    )

    return {
        "direction": direction,
        "strength": strength,
        "slope": round(slope, 2),
        "slope_pct": round(slope_pct, 6),
        "avg_price": round(avg_price, 2),
        "series": series,
        "weeks_analyzed": n,
        "direction_changes": direction_changes,
        "consecutive_weeks": consecutive,
        "message": msg,
    }
