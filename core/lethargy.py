"""Lethargy Detector — market speed slowing at a zone.

Methodology Step 8 (docs/METHODOLOGY_STEPS_5-8.md): Bevor ein Setup bestaetigt
wird, pruefen wir, ob der Markt an einer Zone "muede" wird — Volumen zieht
zurueck, Range komprimiert, Momentum laesst nach. Das ist der typische
Vorbote einer Reaktion an einer Business Zone.

Reine Funktion (P3): gleiche Input-Daten -> gleicher Output (backtestbar).

Heuristik (3-dimensionale Lethargie-Signatur):
  1. Volume Decay:   Kurzfristiges Volumen / laengerfristiges Volumen
  2. Range Compression: Kurzfristige Range / laengerfristige Range
  3. Speed Decay:     Preisbewegung pro Zeiteinheit nimmt ab

Nur wenn mindestens 2 von 3 Dimensionen unter dem Threshold liegen UND
der Preis in der Naehe einer Zone ist (proximity < threshold), wird
Lethargie detektiert.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


# ── Thresholds (tunable via ProfileConfig spaeter) ───────────────────────────
_DEFAULT_SHORT_WINDOW = 5     # bars for "recent" measurement
_DEFAULT_LONG_WINDOW  = 20    # bars for "baseline" measurement
_DEFAULT_DECAY_THRESHOLD = 0.50  # short/long < 0.50 = decay in that dimension
_DEFAULT_PROXIMITY_PCT = 0.005  # 0.5% of price = "near" a zone
_DEFAULT_MIN_DIMENSIONS = 2     # how many dimensions must show decay


def detect_lethargy(
    prices: List[float],
    volumes: List[float],
    timestamps_ms: Optional[List[int]] = None,
    short_window: int = _DEFAULT_SHORT_WINDOW,
    long_window: int = _DEFAULT_LONG_WINDOW,
    decay_threshold: float = _DEFAULT_DECAY_THRESHOLD,
    proximity_pct: float = _DEFAULT_PROXIMITY_PCT,
    min_dimensions: int = _DEFAULT_MIN_DIMENSIONS,
    zone_low: Optional[float] = None,
    zone_high: Optional[float] = None,
) -> Dict[str, Any]:
    """Detect lethargy (market slowing) from recent price/volume data.

    Args:
        prices: Chronological price series (close or mid), most recent LAST.
        volumes: Chronological volume series, same length as prices.
        timestamps_ms: Optional ms timestamps for speed calculation.
        short_window: Bars for recent window.
        long_window: Bars for baseline window.
        decay_threshold: Ratio below which a dimension is considered decaying.
        proximity_pct: Price must be within this fraction of a zone to trigger.
        min_dimensions: How many dimensions must show decay (1-3).
        zone_low: Lower bound of nearest business zone (optional).
        zone_high: Upper bound of nearest business zone (optional).

    Returns:
        {lethargy_detected, lethargy_score (0..1), dimensions_decayed,
         volume_ratio, range_ratio, speed_ratio, zone_proximity,
         at_zone, message}
    """
    n = len(prices)
    if n < long_window:
        return {
            "lethargy_detected": False,
            "lethargy_score": 0.0,
            "dimensions_decayed": 0,
            "volume_ratio": 1.0,
            "range_ratio": 1.0,
            "speed_ratio": 1.0,
            "zone_proximity": None,
            "at_zone": False,
            "message": f"insufficient data ({n} < {long_window})",
        }

    short_prices = prices[-short_window:]
    short_volumes = volumes[-short_window:]
    long_prices = prices[-long_window:]
    long_volumes = volumes[-long_window:]

    # ── 1. Volume Decay ─────────────────────────────────────────────────
    short_vol_avg = sum(short_volumes) / len(short_volumes) if short_volumes else 0.0
    long_vol_avg = sum(long_volumes) / len(long_volumes) if long_volumes else 0.0
    vol_ratio = short_vol_avg / long_vol_avg if long_vol_avg > 0 else 1.0
    vol_decay = vol_ratio < decay_threshold

    # ── 2. Range Compression ────────────────────────────────────────────
    short_range = max(short_prices) - min(short_prices) if short_prices else 0.0
    long_range = max(long_prices) - min(long_prices) if long_prices else 0.0
    range_ratio = short_range / long_range if long_range > 0 else 1.0
    range_decay = range_ratio < decay_threshold

    # ── 3. Speed Decay (price change per unit time) ─────────────────────
    speed_ratio = 1.0
    speed_decay = False
    if timestamps_ms and len(timestamps_ms) >= long_window:
        short_ts = timestamps_ms[-short_window:]
        long_ts = timestamps_ms[-long_window:]
        # Speed = |price change| / time delta
        short_speed = _compute_speed(short_prices, short_ts)
        long_speed = _compute_speed(long_prices, long_ts)
        speed_ratio = short_speed / long_speed if long_speed > 0 else 1.0
        speed_decay = speed_ratio < decay_threshold
    else:
        # Fallback: use price-range-per-bar as speed proxy
        short_range_per_bar = short_range / short_window if short_window > 0 else 0.0
        long_range_per_bar = long_range / long_window if long_window > 0 else 0.0
        speed_ratio = short_range_per_bar / long_range_per_bar if long_range_per_bar > 0 else 1.0
        speed_decay = speed_ratio < decay_threshold

    # ── Zone Proximity ──────────────────────────────────────────────────
    current_price = prices[-1] if prices else 0.0
    zone_proximity: Optional[float] = None
    at_zone = False
    if zone_low is not None and zone_high is not None and current_price > 0:
        zone_center = (zone_low + zone_high) / 2
        dist_to_zone = abs(current_price - zone_center)
        threshold_dist = current_price * proximity_pct
        zone_proximity = round(1.0 - min(dist_to_zone / threshold_dist, 1.0), 4)
        at_zone = dist_to_zone <= threshold_dist

    # ── Aggregate ───────────────────────────────────────────────────────
    dims = sum([vol_decay, range_decay, speed_decay])
    lethargy_detected = dims >= min_dimensions and at_zone

    # Score: weighted blend of decay ratios (inverted so 1 = max lethargy)
    lethargy_score = round(
        1.0 - (vol_ratio * 0.4 + range_ratio * 0.3 + speed_ratio * 0.3), 4
    ) if lethargy_detected else 0.0
    lethargy_score = max(0.0, min(1.0, lethargy_score))

    dim_names = []
    if vol_decay:
        dim_names.append("volume")
    if range_decay:
        dim_names.append("range")
    if speed_decay:
        dim_names.append("speed")

    msg = (
        f"Lethargy {'DETECTED' if lethargy_detected else 'not detected'} "
        f"(score={lethargy_score:.2f}, dims={dim_names or 'none'}, "
        f"vol={vol_ratio:.2f}, range={range_ratio:.2f}, speed={speed_ratio:.2f}, "
        f"at_zone={at_zone})"
    )

    return {
        "lethargy_detected": lethargy_detected,
        "lethargy_score": lethargy_score,
        "dimensions_decayed": dims,
        "dimensions_decayed_names": dim_names,
        "volume_ratio": round(vol_ratio, 4),
        "range_ratio": round(range_ratio, 4),
        "speed_ratio": round(speed_ratio, 4),
        "zone_proximity": zone_proximity,
        "at_zone": at_zone,
        "message": msg,
    }


def _compute_speed(prices: List[float], timestamps_ms: List[int]) -> float:
    """Compute absolute price change per second over the window."""
    if len(prices) < 2 or len(timestamps_ms) < 2:
        return 0.0
    dt_s = (timestamps_ms[-1] - timestamps_ms[0]) / 1000.0
    if dt_s <= 0:
        return 0.0
    dp = abs(prices[-1] - prices[0])
    return dp / dt_s


class LethargyDetector:
    """Stateful wrapper: maintains rolling price/volume buffers, checks
    lethargy relative to a configurable business zone.

    Trades and zone knowledge typically arrive on separate loops (trades
    stream continuously; the nearest business zone is only known once per
    publish cycle, after ZoneRegistry.snapshot() runs). set_zone() lets the
    zone be updated independently of ingest() — ingest() falls back to the
    last zone set via set_zone() when not given explicit bounds, and assess()
    lets a caller get the current reading without feeding a new trade.

    Usage:
        det = LethargyDetector()
        for trade in trades:
            det.ingest(trade.price, trade.size, trade.timestamp)
        det.set_zone(zone_low, zone_high)
        result = det.assess()
        if result["lethargy_detected"]:
            print(f"Lethargy at zone: {result['message']}")
    """

    def __init__(
        self,
        short_window: int = _DEFAULT_SHORT_WINDOW,
        long_window: int = _DEFAULT_LONG_WINDOW,
        decay_threshold: float = _DEFAULT_DECAY_THRESHOLD,
        proximity_pct: float = _DEFAULT_PROXIMITY_PCT,
        min_dimensions: int = _DEFAULT_MIN_DIMENSIONS,
    ) -> None:
        self.short_window = short_window
        self.long_window = long_window
        self.decay_threshold = decay_threshold
        self.proximity_pct = proximity_pct
        self.min_dimensions = min_dimensions

        self._prices: List[float] = []
        self._volumes: List[float] = []
        self._timestamps: List[int] = []
        self._maxlen = max(long_window * 2, 200)  # ring buffer cap
        self._zone_low: Optional[float] = None
        self._zone_high: Optional[float] = None

    def ingest(
        self,
        price: float,
        size: float,
        timestamp_ms: int,
        zone_low: Optional[float] = None,
        zone_high: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Feed one trade and return current lethargy assessment.

        zone_low/zone_high default to the last value passed to set_zone()
        when omitted.
        """
        self._prices.append(price)
        self._volumes.append(size)
        self._timestamps.append(timestamp_ms)

        # Ring buffer cap
        if len(self._prices) > self._maxlen:
            self._prices = self._prices[-self._maxlen:]
            self._volumes = self._volumes[-self._maxlen:]
            self._timestamps = self._timestamps[-self._maxlen:]

        zl = zone_low if zone_low is not None else self._zone_low
        zh = zone_high if zone_high is not None else self._zone_high
        return self._detect(zone_low=zl, zone_high=zh)

    def assess(
        self,
        zone_low: Optional[float] = None,
        zone_high: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Compute the current lethargy reading without ingesting a new trade.

        Used at publish time once the nearest business zone is known —
        trades arrive on a separate loop that has no zone context per-tick.
        zone_low/zone_high default to the last value passed to set_zone().
        """
        zl = zone_low if zone_low is not None else self._zone_low
        zh = zone_high if zone_high is not None else self._zone_high
        return self._detect(zone_low=zl, zone_high=zh)

    def _detect(self, zone_low: Optional[float], zone_high: Optional[float]) -> Dict[str, Any]:
        return detect_lethargy(
            prices=self._prices,
            volumes=self._volumes,
            timestamps_ms=self._timestamps,
            short_window=self.short_window,
            long_window=self.long_window,
            decay_threshold=self.decay_threshold,
            proximity_pct=self.proximity_pct,
            min_dimensions=self.min_dimensions,
            zone_low=zone_low,
            zone_high=zone_high,
        )

    def set_zone(self, zone_low: Optional[float], zone_high: Optional[float]) -> None:
        """Update the nearest zone — used as the default for ingest()/assess()
        when they're not called with explicit zone bounds."""
        self._zone_low = zone_low
        self._zone_high = zone_high

    def reset(self) -> None:
        """Clear all buffers."""
        self._prices.clear()
        self._volumes.clear()
        self._timestamps.clear()
