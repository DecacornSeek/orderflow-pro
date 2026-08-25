"""Daily Road Map — deterministische Verhaltens-Erwartung + Setup-Matrix.

Methodology Step 7 (docs/METHODOLOGY_STEPS_5-8.md): der strategische Plan,
wie mit den Business Zones (Step 6) interagiert wird. Reine Funktion (P3):
gleicher Kontext -> gleicher Plan, live wie im Backtest. Kein LLM-Urteil —
der Signal Agent darf den Plan nur noch in Sprache uebersetzen (P2).

Regeln:
  day_type aus Session- + Weekly-Regime:
    session balanced                       -> "balance"
    session imbalanced_up,   weekly nicht imbalanced_down -> "trend_up"
    session imbalanced_down, weekly nicht imbalanced_up   -> "trend_down"
    Session gegen Weekly (Konflikt)        -> "conflicted"
    session imbalanced (richtungslos)      -> "transition"
    sonst / keine Daten                    -> "neutral"

  Erwartete Geschwindigkeit an einer Zone (Art -> Verhalten):
    single_print -> "fast"       (Low-Volume-Luecke, schneller Durchlauf)
    hvn, poc     -> "rotation"   (Akzeptanz, Seitwaertsverhalten)
    vah, val     -> "reaction"   (Value-Kante, Reaktion erwartet)

  Setup-Matrix (welche Setups heute erlaubt sind):
    balance    -> Counter-Trades an den Zonen-Kanten (D-Profil als Proof)
    trend_*    -> Continuation-Breakouts + Pullback-to-Value (P/B als Proof)
    transition -> nur mit Bestaetigung handeln
    conflicted -> beiseite stehen
    neutral    -> beiseite stehen
"""

from typing import Any, Dict, List, Optional

_SPEED_BY_KIND = {
    "single_print": "fast",
    "hvn": "rotation",
    "poc": "rotation",
    "vah": "reaction",
    "val": "reaction",
}

SETUP_MATRIX: Dict[str, List[str]] = {
    "balance":    ["fade_edge_counter"],
    "trend_up":   ["breakout_continuation_long", "pullback_to_value_long"],
    "trend_down": ["breakout_continuation_short", "pullback_to_value_short"],
    "transition": ["wait_for_confirmation"],
    "conflicted": ["stand_aside"],
    "neutral":    ["stand_aside"],
}


def _day_type(session_regime: Optional[str], weekly_regime: Optional[str]) -> str:
    s = session_regime or "neutral"
    w = weekly_regime or "neutral"
    if s == "balanced":
        return "balance"
    if s == "imbalanced_up":
        return "conflicted" if w == "imbalanced_down" else "trend_up"
    if s == "imbalanced_down":
        return "conflicted" if w == "imbalanced_up" else "trend_down"
    if s == "imbalanced":
        return "transition"
    return "neutral"


def _zone_speed(zone: Optional[Dict[str, Any]]) -> Optional[str]:
    if not zone:
        return None
    return _SPEED_BY_KIND.get(zone.get("kind"), "reaction")


def build_road_map(session_ctx: Optional[Dict[str, Any]],
                   weekly_ctx: Optional[Dict[str, Any]],
                   zones_ctx: Optional[Dict[str, Any]],
                   price: Optional[float] = None) -> Dict[str, Any]:
    """Road Map aus Kontext-Schichten bauen.

    Args:
        session_ctx: session_context dict (braucht "regime").
        weekly_ctx: weekly_context dict (braucht "regime").
        zones_ctx: ZoneRegistry.snapshot() dict (zone_at/below/above).
        price: aktueller Preis (nur informativ durchgereicht).

    Returns:
        {day_type, dominant_direction, allowed_setups,
         point_a, point_b, expected_speed_above, expected_speed_below,
         price}
        point_a = Ausgangszone (enthaltend bzw. entgegen der Richtung),
        point_b = Zielzone in dominanter Richtung. Bei balance:
        point_a = untere Kante, point_b = obere Kante der Range.
    """
    session_ctx = session_ctx or {}
    weekly_ctx = weekly_ctx or {}
    zones_ctx = zones_ctx or {}

    day_type = _day_type(session_ctx.get("regime"), weekly_ctx.get("regime"))
    direction = ("up" if day_type == "trend_up"
                 else "down" if day_type == "trend_down" else "none")

    zone_at = zones_ctx.get("zone_at")
    zone_below = zones_ctx.get("zone_below")
    zone_above = zones_ctx.get("zone_above")

    if direction == "up":
        point_a = zone_at or zone_below
        point_b = zone_above
    elif direction == "down":
        point_a = zone_at or zone_above
        point_b = zone_below
    else:
        # Balance/neutral: Range zwischen unterer und oberer Zone
        point_a = zone_below
        point_b = zone_above

    return {
        "day_type": day_type,
        "dominant_direction": direction,
        "allowed_setups": list(SETUP_MATRIX[day_type]),
        "point_a": point_a,
        "point_b": point_b,
        "expected_speed_above": _zone_speed(zone_above),
        "expected_speed_below": _zone_speed(zone_below),
        "price": price,
    }
