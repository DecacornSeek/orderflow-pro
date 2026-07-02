"""Profil-Struktur — Single Prints, schwache/starke Extreme, Double Distribution.

Methodology Step 5 (docs/METHODOLOGY_STEPS_5-8.md): Strukturmerkmale eines
Volume Profiles, die zeigen wo grosse Teilnehmer aktiv waren.

Alle Funktionen sind rein (P3): VAP dict rein, dict raus — identisches
Ergebnis live und im Replay/Backtest. Kein Wall-Clock, kein State.

Heuristiken (Market-Profile-Konvention):
  Single Print: innere Bucket-Folge mit Volumen <= single_print_max_pct des
      POC-Volumens — Preis wurde schnell durchlaufen, Markt "schuldet" dort
      einen Besuch ("Repair").
  Starkes Extrem: duenner Rand (Volumen am Extrem << Profil-Durchschnitt)
      = schnelle institutionelle Ablehnung, Auktion beendet.
  Schwaches Extrem: fetter Rand (Volumen am Extrem ~ Profilkoerper)
      = Auktion unfertig ("poor high/low"), Revisit wahrscheinlich.

Alle Schwellwerte kommen aus ProfileConfig.
"""

from typing import Any, Dict, List, Optional

from core.profile_config import ProfileConfig, PROFILE_CONFIG
from core.profile_shape import classify_shape


# ---------------------------------------------------------------------------
# Single Prints
# ---------------------------------------------------------------------------

def find_single_prints(vap: Optional[Dict[int, float]],
                       max_pct: Optional[float] = None,
                       min_run: Optional[int] = None) -> List[Dict[str, Any]]:
    """Innere Niedrigst-Volumen-Folgen — vom Markt schnell durchlaufene Zonen.

    Approximation aus dem VAP (echte Single Prints brauchen TPO/Zeitdaten):
    zusammenhaengende INNERE Buckets mit Volumen <= max_pct * POC-Volumen.
    Rand-Buckets zaehlen nicht — dort ist niedriges Volumen normal (Tails).

    Returns:
        Liste von {price_low, price_high, bucket_count, volume} —
        aufsteigend nach price_low. Leer wenn kein VAP oder zu klein.
    """
    cfg = PROFILE_CONFIG
    if max_pct is None:
        max_pct = cfg.single_print_max_pct
    if min_run is None:
        min_run = cfg.single_print_min_run
    if not vap or len(vap) < 3:
        return []

    buckets = sorted(vap.keys())
    poc_vol = max(vap.values())
    if poc_vol <= 0:
        return []
    threshold = poc_vol * max_pct

    prints: List[Dict[str, Any]] = []
    run: List[int] = []
    # Nur innere Buckets (erster + letzter sind Tails, keine Single Prints)
    for b in buckets[1:-1]:
        if vap[b] <= threshold:
            run.append(b)
        else:
            if len(run) >= min_run:
                prints.append(_run_to_zone(run, vap))
            run = []
    if len(run) >= min_run:
        prints.append(_run_to_zone(run, vap))

    return prints


def _run_to_zone(run: List[int], vap: Dict[int, float]) -> Dict[str, Any]:
    return {
        "price_low": run[0],
        "price_high": run[-1],
        "bucket_count": len(run),
        "volume": round(sum(vap[b] for b in run), 4),
    }


# ---------------------------------------------------------------------------
# Schwache/starke Extreme
# ---------------------------------------------------------------------------

def classify_extremes(vap: Optional[Dict[int, float]],
                      edge_buckets: Optional[int] = None,
                      strong_max: Optional[float] = None,
                      weak_min: Optional[float] = None) -> Dict[str, Any]:
    """Sind Hoch und Tief des Profils schwach oder stark geformt?

    Vergleicht das Durchschnittsvolumen der edge_buckets Rand-Buckets mit dem
    Durchschnittsvolumen des Profilkoerpers (alle uebrigen Buckets):
      ratio <= strong_max -> "strong"  (duenner Tail, institutionelle Ablehnung)
      ratio >= weak_min   -> "weak"    (poor high/low, Revisit wahrscheinlich)
      dazwischen          -> "moderate"

    Returns:
        {high_strength, low_strength, high_edge_ratio, low_edge_ratio}
        — "unknown" wenn Profil zu klein (< min_buckets_for_extremes Buckets).
    """
    cfg = PROFILE_CONFIG
    if edge_buckets is None:
        edge_buckets = cfg.extreme_edge_buckets
    if strong_max is None:
        strong_max = cfg.strong_extreme_max_ratio
    if weak_min is None:
        weak_min = cfg.weak_extreme_min_ratio
    result: Dict[str, Any] = {
        "high_strength": "unknown", "low_strength": "unknown",
        "high_edge_ratio": None, "low_edge_ratio": None,
    }
    if not vap or len(vap) < cfg.min_buckets_for_extremes:
        return result

    buckets = sorted(vap.keys())
    top_edge = buckets[-edge_buckets:]
    bottom_edge = buckets[:edge_buckets]
    body = buckets[edge_buckets:-edge_buckets]
    if not body:
        return result

    body_avg = sum(vap[b] for b in body) / len(body)
    if body_avg <= 0:
        return result

    def classify(edge: List[int]) -> tuple:
        edge_avg = sum(vap[b] for b in edge) / len(edge)
        ratio = edge_avg / body_avg
        if ratio <= strong_max:
            return "strong", round(ratio, 4)
        if ratio >= weak_min:
            return "weak", round(ratio, 4)
        return "moderate", round(ratio, 4)

    result["high_strength"], result["high_edge_ratio"] = classify(top_edge)
    result["low_strength"], result["low_edge_ratio"] = classify(bottom_edge)
    return result


# ---------------------------------------------------------------------------
# Double Distribution
# ---------------------------------------------------------------------------

def detect_double_distribution(vap: Optional[Dict[int, float]]) -> Optional[Dict[str, Any]]:
    """Zwei Akzeptanzbereiche in einem Profil (Shape "B" aus profile_shape).

    Der Markt hat innerhalb der Periode neu bewertet — die Zone zwischen
    den beiden Verteilungen ist typischerweise eine Low-Volume-Bruecke.

    Returns:
        {upper_poc, lower_poc, separation_buckets, bridge} oder None wenn
        keine Double Distribution. bridge = Single-Print-Zone zwischen den
        POCs falls vorhanden, sonst None.
    """
    if not vap:
        return None
    shape = classify_shape(vap)
    if shape["shape"] != "B" or shape["secondary_peak_bucket"] is None:
        return None

    p1 = shape["top_peak_bucket"]
    p2 = shape["secondary_peak_bucket"]
    upper, lower = max(p1, p2), min(p1, p2)

    # Bruecke: Single Print zwischen den beiden Verteilungen
    bridge = None
    for sp in find_single_prints(vap):
        if lower < sp["price_low"] and sp["price_high"] < upper:
            bridge = sp
            break

    return {
        "upper_poc": upper,
        "lower_poc": lower,
        "separation_buckets": upper - lower,
        "bridge": bridge,
    }


# ---------------------------------------------------------------------------
# Sammel-Kontext (eine Zeile im Feature-Vektor)
# ---------------------------------------------------------------------------

def structure_context(vap: Optional[Dict[int, float]]) -> Dict[str, Any]:
    """Flacher Feature-Kontext fuer Logger/Strategy Engine.

    Returns:
        {n_single_prints, single_prints, high_strength, low_strength,
         is_double_distribution, dist_upper_poc, dist_lower_poc}
    """
    prints = find_single_prints(vap)
    extremes = classify_extremes(vap)
    dd = detect_double_distribution(vap)
    return {
        "n_single_prints": len(prints),
        "single_prints": prints,
        "high_strength": extremes["high_strength"],
        "low_strength": extremes["low_strength"],
        "is_double_distribution": dd is not None,
        "dist_upper_poc": dd["upper_poc"] if dd else None,
        "dist_lower_poc": dd["lower_poc"] if dd else None,
    }
