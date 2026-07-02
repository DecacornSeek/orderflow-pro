"""Business Zones — smarte Support/Resistance-Zonen aus archivierten Profilen.

Methodology Step 6 (docs/METHODOLOGY_STEPS_5-8.md): Zonen, an denen eine
Reaktion grosser Teilnehmer erwartet wird — abgeleitet aus POC/VAH/VAL,
HVN und Single Prints vergangener Profile. Zonen, die von mehreren Profilen
bestaetigt werden (recurrence), sind staerker.

Point A -> Point B: nearest_zones() liefert die naechste Zone unter und
ueber dem aktuellen Preis — der erwartete Pfad des Marktes.

build_zones() ist rein (Profile rein, Zonen raus) und damit backtestbar;
ZoneRegistry ist der duenne Live-Wrapper mit tested/repaired-Zustand.

Alle Schwellwerte kommen aus ProfileConfig.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.profile_config import ProfileConfig, PROFILE_CONFIG, resolve_config
from core.volume_profile import BUCKET, ProfileSnapshot
from core.composite_profile import find_hvn_zones
from core.profile_structure import find_single_prints

# Prioritaet fuer Sortierung bei gleicher Recurrence
_KIND_PRIORITY = {"poc": 0, "hvn": 1, "vah": 2, "val": 2, "single_print": 3}


@dataclass
class Zone:
    price_low: int
    price_high: int
    kind: str                      # poc | vah | val | hvn | single_print
    volume: float = 0.0
    recurrence: int = 1            # von wie vielen Profilen bestaetigt
    state: str = "untested"        # untested | tested | repaired
    source_labels: List[str] = field(default_factory=list)
    # Kumulativ vom Preis durchlaufener Bereich (auf Zone geclippt) —
    # Repair kann sich ueber mehrere Preis-Segmente aufbauen
    seen_low: Optional[float] = None
    seen_high: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "price_low": self.price_low, "price_high": self.price_high,
            "kind": self.kind, "volume": round(self.volume, 4),
            "recurrence": self.recurrence, "state": self.state,
            "sources": list(self.source_labels),
        }


def _overlaps(a: Zone, b: Zone, gap_buckets: int = 1) -> bool:
    gap = gap_buckets * BUCKET
    return a.price_low <= b.price_high + gap and b.price_low <= a.price_high + gap


def build_zones(profiles: List[ProfileSnapshot],
                merge_gap_buckets: Optional[int] = None) -> List[Zone]:
    """Zonen aus archivierten Profilen ableiten und nach Art verschmelzen.

    Pro Profil: POC/VAH/VAL als 1-Bucket-Level-Zonen, HVN-Bereiche und
    Single Prints als Range-Zonen. Ueberlappende Zonen gleicher Art werden
    verschmolzen; recurrence zaehlt die bestaetigenden Profile.

    Reine Funktion: gleiche Profile -> gleiche Zonen (Backtest-tauglich).
    """
    if merge_gap_buckets is None:
        merge_gap_buckets = PROFILE_CONFIG.merge_gap_buckets
    raw: List[Zone] = []
    for p in profiles:
        label = p.label
        if p.poc is not None:
            raw.append(Zone(p.poc, p.poc + BUCKET, "poc",
                            volume=p.vap.get(p.poc, 0.0), source_labels=[label]))
        if p.value_area_high is not None:
            raw.append(Zone(p.value_area_high, p.value_area_high + BUCKET, "vah",
                            volume=p.vap.get(p.value_area_high, 0.0), source_labels=[label]))
        if p.value_area_low is not None:
            raw.append(Zone(p.value_area_low, p.value_area_low + BUCKET, "val",
                            volume=p.vap.get(p.value_area_low, 0.0), source_labels=[label]))
        for hvn in find_hvn_zones(p.vap):
            raw.append(Zone(int(hvn["price_low"]), int(hvn["price_high"]) + BUCKET, "hvn",
                            volume=hvn["volume"], source_labels=[label]))
        for sp in find_single_prints(p.vap):
            raw.append(Zone(int(sp["price_low"]), int(sp["price_high"]) + BUCKET, "single_print",
                            volume=sp["volume"], source_labels=[label]))

    # Verschmelzen: pro Art, sortiert nach price_low
    merged: List[Zone] = []
    for kind in sorted({z.kind for z in raw}):
        same = sorted((z for z in raw if z.kind == kind), key=lambda z: z.price_low)
        current: Optional[Zone] = None
        for z in same:
            if current is not None and _overlaps(current, z, merge_gap_buckets):
                current.price_low = min(current.price_low, z.price_low)
                current.price_high = max(current.price_high, z.price_high)
                current.volume += z.volume
                # Recurrence = Anzahl verschiedener Quell-Profile
                for lbl in z.source_labels:
                    if lbl not in current.source_labels:
                        current.source_labels.append(lbl)
                current.recurrence = len(current.source_labels)
            else:
                if current is not None:
                    merged.append(current)
                current = Zone(z.price_low, z.price_high, z.kind, volume=z.volume,
                               recurrence=len(z.source_labels),
                               source_labels=list(z.source_labels))
        if current is not None:
            merged.append(current)

    # Staerkste zuerst: Recurrence, dann Volumen, dann Art-Prioritaet
    merged.sort(key=lambda z: (-z.recurrence, -z.volume,
                               _KIND_PRIORITY.get(z.kind, 9), z.price_low))
    return merged


def nearest_zones(zones: List[Zone], price: float) -> Dict[str, Optional[Dict[str, Any]]]:
    """Naechste Zone unter und ueber dem Preis — der Point-A->Point-B-Pfad.

    Eine Zone, die den Preis enthaelt, wird als "zone_at" gemeldet.
    """
    below: Optional[Zone] = None
    above: Optional[Zone] = None
    at: Optional[Zone] = None
    for z in zones:
        if z.price_low <= price <= z.price_high:
            if at is None or z.recurrence > at.recurrence:
                at = z
        elif z.price_high < price:
            if below is None or z.price_high > below.price_high:
                below = z
        elif z.price_low > price:
            if above is None or z.price_low < above.price_low:
                above = z
    return {
        "zone_at": at.to_dict() if at else None,
        "zone_below": below.to_dict() if below else None,
        "zone_above": above.to_dict() if above else None,
    }


class ZoneRegistry:
    """Live-Wrapper: haelt Zonen + tested/repaired-Zustand ueber Preis-Updates.

    Usage:
        reg = ZoneRegistry()
        reg.rebuild(session.get_archived_profiles() + weekly.get_archived_profiles())
        reg.update_price(mid_price)      # pro Tick/Publish
        ctx = reg.snapshot(mid_price)    # fuer AGGREGATED payload
    """

    def __init__(self, config: Optional[ProfileConfig] = None,
                 max_zones: Optional[int] = None) -> None:
        cfg = resolve_config(config, max_zones=max_zones)
        self.config = cfg
        self.max_zones = cfg.max_zones
        self._zones: List[Zone] = []
        self._last_price: Optional[float] = None

    def rebuild(self, profiles: List[ProfileSnapshot]) -> None:
        """Zonen neu ableiten; Zustand bestehender Zonen bleibt erhalten,
        wenn Range und Art uebereinstimmen."""
        old = {(z.kind, z.price_low, z.price_high): z for z in self._zones}
        self._zones = build_zones(profiles)
        for z in self._zones:
            prev = old.get((z.kind, z.price_low, z.price_high))
            if prev is not None:
                z.state = prev.state
                z.seen_low = prev.seen_low
                z.seen_high = prev.seen_high

    def update_price(self, price: Optional[float]) -> None:
        """Zonen-Zustand fortschreiben.

        tested:   das Preis-Segment seit dem letzten Update beruehrt die Zone.
        repaired: der kumulativ durchlaufene Bereich deckt die GESAMTE Zone ab
                  (nur single_print — der Markt hat die Luecke "repariert";
                  darf sich ueber mehrere Segmente aufbauen).
        """
        if price is None:
            return
        prev = self._last_price if self._last_price is not None else price
        self._last_price = price
        seg_lo, seg_hi = min(prev, price), max(prev, price)

        for z in self._zones:
            if z.state == "repaired":
                continue
            overlap_lo = max(seg_lo, z.price_low)
            overlap_hi = min(seg_hi, z.price_high)
            if overlap_lo > overlap_hi:
                continue  # keine Beruehrung
            z.seen_low = overlap_lo if z.seen_low is None else min(z.seen_low, overlap_lo)
            z.seen_high = overlap_hi if z.seen_high is None else max(z.seen_high, overlap_hi)
            if (z.kind == "single_print"
                    and z.seen_low <= z.price_low and z.seen_high >= z.price_high):
                z.state = "repaired"
            elif z.state == "untested":
                z.state = "tested"

    def snapshot(self, price: Optional[float]) -> Dict[str, Any]:
        """Kontext fuer den AGGREGATED payload / Feature-Vektor."""
        top = self._zones[:self.max_zones]
        ctx: Dict[str, Any] = {
            "zones": [z.to_dict() for z in top],
            "zone_count": len(self._zones),
            "n_unrepaired_single_prints": sum(
                1 for z in self._zones
                if z.kind == "single_print" and z.state != "repaired"
            ),
        }
        if price is not None:
            ctx.update(nearest_zones(self._zones, price))
        else:
            ctx.update({"zone_at": None, "zone_below": None, "zone_above": None})
        return ctx
