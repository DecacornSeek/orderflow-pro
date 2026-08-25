"""
Tests for core/lethargy.py — Lethargy Detector (Methodology Step 8).

Verifies:
  - Volume decay detection
  - Range compression detection
  - Speed decay detection
  - Zone proximity gating
  - Minimum dimensions requirement (2 of 3)
  - Stateful LethargyDetector wrapper
"""

import sys

import pytest

sys.path.insert(0, ".")

from core.lethargy import detect_lethargy, LethargyDetector


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_data(n: int, base_price: float = 50000.0, base_vol: float = 1.0,
               price_step: float = 10.0, vol_step: float = 0.0) -> tuple:
    """Generate n bars of linear price/volume data."""
    prices = [base_price + i * price_step for i in range(n)]
    volumes = [base_vol + i * vol_step for i in range(n)]
    return prices, volumes


# ── Pure function tests ──────────────────────────────────────────────────────

class TestDetectLethargy:

    def test_insufficient_data(self):
        """Less than long_window bars → insufficient_data."""
        prices, volumes = _make_data(10)
        result = detect_lethargy(prices, volumes, long_window=20)
        assert result["lethargy_detected"] is False
        assert "insufficient data" in result["message"]

    def test_no_zone_no_lethargy(self):
        """Even if all dimensions decay, without zone proximity lethargy not detected."""
        prices, volumes = _make_data(30, price_step=10.0, base_vol=1.0, vol_step=-0.2)
        # Make short window have tiny range and volume (decay)
        prices[-5:] = [prices[-6]] * 5  # flat price
        volumes[-5:] = [0.01] * 5      # tiny volume
        result = detect_lethargy(prices, volumes)
        # Dimensions may decay but at_zone=False → no lethargy
        assert result["lethargy_detected"] is False
        assert result["at_zone"] is False

    def test_lethargy_at_zone(self):
        """All dimensions decay AND price is near a zone → lethargy detected."""
        n = 30
        prices, volumes = _make_data(n, base_price=50000.0, price_step=5.0, base_vol=2.0)
        # Recent bars: flat price, tiny volume
        prices[-5:] = [50000.0] * 5
        volumes[-5:] = [0.01] * 5
        result = detect_lethargy(prices, volumes, zone_low=49900, zone_high=50100)
        assert result["lethargy_detected"] is True
        assert result["lethargy_score"] > 0.0
        assert result["at_zone"] is True
        assert result["dimensions_decayed"] >= 2

    def test_only_one_dimension_decay_no_lethargy(self):
        """One decaying dimension is not enough (min_dimensions=2)."""
        # Use oscillating prices so short/long range ratio ≈ 1.0
        # Only volume shows decay.
        import math
        n = 30
        prices = [50000.0 + 200.0 * math.sin(i * 0.8) for i in range(n)]
        volumes = [1.0] * 25 + [0.01] * 5  # only volume decays in short window
        result = detect_lethargy(prices, volumes, zone_low=49900, zone_high=50100,
                                 min_dimensions=2,
                                 short_window=5, long_window=25)
        # Only volume should decay; range and speed should stay proportional
        assert result["dimensions_decayed"] < 2
        assert result["lethargy_detected"] is False

    def test_volume_ratio_calculation(self):
        """Volume ratio = short_window_avg / long_window_avg."""
        n = 25
        prices = [50000.0 + i * 10 for i in range(n)]
        volumes = [5.0] * 20 + [0.5] * 5  # short=0.5, long≈4.1 → ratio≈0.12
        result = detect_lethargy(prices, volumes, short_window=5, long_window=20)
        assert result["volume_ratio"] < 0.5  # significant decay

    def test_range_ratio_calculation(self):
        """Range ratio = short_range / long_range."""
        n = 25
        prices = list(range(50000, 50250, 10))  # steady uptrend
        prices[-5:] = [50240] * 5  # flat, no range
        volumes = [1.0] * 25
        result = detect_lethargy(prices, volumes, short_window=5, long_window=20)
        # Short range should be near zero, long range > 0
        assert result["range_ratio"] < 0.5

    def test_speed_ratio_without_timestamps(self):
        """When no timestamps, speed = range_per_bar ratio."""
        n = 25
        prices = list(range(50000, 50250, 10))
        prices[-5:] = [50240] * 5  # flat
        volumes = [1.0] * 25
        result = detect_lethargy(prices, volumes, short_window=5, long_window=20)
        assert result["speed_ratio"] < 0.5

    def test_speed_ratio_with_timestamps(self):
        """Speed uses actual time deltas when timestamps provided."""
        n = 25
        prices = list(range(50000, 50250, 10))
        prices[-5:] = [50240] * 5
        volumes = [1.0] * 25
        timestamps = list(range(0, n * 1000, 1000))  # 1 bar/sec
        result = detect_lethargy(prices, volumes, timestamps_ms=timestamps,
                                 short_window=5, long_window=20)
        assert result["speed_ratio"] < 0.5

    def test_zone_proximity_far(self):
        """Price far from zone → at_zone=False."""
        prices, volumes = _make_data(30, base_price=50000.0)
        result = detect_lethargy(prices, volumes, zone_low=60000, zone_high=61000)
        assert result["at_zone"] is False
        assert result["zone_proximity"] is not None
        assert result["zone_proximity"] < 0.5

    def test_zone_proximity_near(self):
        """Price within 0.5% of zone center → at_zone=True."""
        prices, volumes = _make_data(30, base_price=50000.0)
        prices[-1] = 50050  # within 0.5% of zone center at 50000
        result = detect_lethargy(prices, volumes, zone_low=49900, zone_high=50100)
        assert result["at_zone"] is True

    def test_dimension_names(self):
        """dimensions_decayed_names lists which dimensions decayed."""
        n = 30
        prices, volumes = _make_data(n, base_price=50000.0)
        prices[-5:] = [50000.0] * 5
        volumes[-5:] = [0.01] * 5
        result = detect_lethargy(prices, volumes, zone_low=49900, zone_high=50100)
        names = result["dimensions_decayed_names"]
        assert "volume" in names
        assert "range" in names


# ── Stateful LethargyDetector tests ──────────────────────────────────────────

class TestLethargyDetector:

    def test_stateful_ingest_accumulates(self):
        det = LethargyDetector(short_window=5, long_window=20)
        for i in range(25):
            det.ingest(price=50000.0 + i * 10, size=1.0,
                       timestamp_ms=i * 1000)
        assert len(det._prices) == 25
        assert len(det._volumes) == 25
        assert len(det._timestamps) == 25

    def test_ring_buffer_cap(self):
        det = LethargyDetector(short_window=5, long_window=20)
        for i in range(500):
            det.ingest(price=50000.0, size=1.0, timestamp_ms=i * 1000)
        assert len(det._prices) <= det._maxlen

    def test_stateful_lethargy_detection(self):
        det = LethargyDetector(short_window=5, long_window=20,
                               decay_threshold=0.5, proximity_pct=0.01)
        # Feed 20 normal bars
        for i in range(20):
            det.ingest(price=50000.0 + i * 10, size=2.0,
                       timestamp_ms=i * 1000)
        # Feed 5 flat/tiny bars near zone
        for i in range(5):
            det.ingest(price=50050.0, size=0.01,
                       timestamp_ms=(20 + i) * 1000)
        result = det.ingest(price=50050.0, size=0.01,
                            timestamp_ms=25000,
                            zone_low=49900, zone_high=50100)
        assert result["lethargy_detected"] is True

    def test_reset_clears_buffers(self):
        det = LethargyDetector()
        for i in range(30):
            det.ingest(price=50000.0, size=1.0, timestamp_ms=i * 1000)
        det.reset()
        assert len(det._prices) == 0
        assert len(det._volumes) == 0
        assert len(det._timestamps) == 0


if __name__ == "__main__":
    # Repo convention is `python tests/test_X.py` — without this guard,
    # running this pytest-class file that way silently executes zero tests.
    sys.exit(pytest.main([__file__, "-q"]))
