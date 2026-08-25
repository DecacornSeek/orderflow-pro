"""
Tests for core/vpoc_trend.py — Multi-Week VPOC Trend Series.

Verifies:
  - VPOC series extraction from ProfileSnapshots
  - Missing POC handling (require_poc=True/False)
  - Trend classification: rising, falling, flattening
  - Insufficient data handling (< min_weeks)
  - Slope calculation via linear regression
  - Direction change counting
  - Consecutive weeks in current direction
"""

import sys

import pytest

sys.path.insert(0, ".")

from core.volume_profile import ProfileSnapshot
from core.vpoc_trend import build_vpoc_series, classify_vpoc_trend


# ── Helpers ──────────────────────────────────────────────────────────────────

def _snapshot(label: str, poc: int = None, timestamp: int = 0, **kwargs) -> ProfileSnapshot:
    """Create a minimal ProfileSnapshot for testing."""
    return ProfileSnapshot(
        label=label,
        timestamp=timestamp,
        ohlc=kwargs.get("ohlc", {"open": None, "high": None, "low": None, "close": None}),
        poc=poc,
        value_area_high=kwargs.get("va_high"),
        value_area_low=kwargs.get("va_low"),
        total_volume=kwargs.get("total_volume", 100.0),
        bucket_count=kwargs.get("bucket_count", 5),
        vap=kwargs.get("vap", {}),
        poc_drift=kwargs.get("poc_drift", []),
    )


def _make_rising_weeks(n: int, start_poc: int = 50000, step: int = 100) -> list:
    """Create n weeks with steadily rising POC."""
    return [
        _snapshot(f"Week-2026-W{i+1:02d}", poc=start_poc + i * step,
                  timestamp=(i + 1) * 1000)
        for i in range(n)
    ]


def _make_falling_weeks(n: int, start_poc: int = 50000, step: int = 100) -> list:
    """Create n weeks with steadily falling POC."""
    return [
        _snapshot(f"Week-2026-W{i+1:02d}", poc=start_poc - i * step,
                  timestamp=(i + 1) * 1000)
        for i in range(n)
    ]


def _make_flat_weeks(n: int, poc: int = 50000, noise: int = 10) -> list:
    """Create n weeks with roughly flat POC (small noise)."""
    import random
    random.seed(42)
    return [
        _snapshot(f"Week-2026-W{i+1:02d}",
                  poc=poc + random.randint(-noise, noise),
                  timestamp=(i + 1) * 1000)
        for i in range(n)
    ]


# ── VPOC Series Extraction ───────────────────────────────────────────────────

class TestBuildVpocSeries:

    def test_extracts_pocs_in_order(self):
        profiles = _make_rising_weeks(5)
        series = build_vpoc_series(profiles)
        assert len(series) == 5
        assert [s["poc"] for s in series] == [50000, 50100, 50200, 50300, 50400]

    def test_skips_none_poc_when_require_poc_true(self):
        profiles = [
            _snapshot("W1", poc=50000),
            _snapshot("W2", poc=None),   # no POC
            _snapshot("W3", poc=50200),
        ]
        series = build_vpoc_series(profiles, require_poc=True)
        assert len(series) == 2
        assert series[0]["poc"] == 50000
        assert series[1]["poc"] == 50200

    def test_empty_profiles_returns_empty(self):
        assert build_vpoc_series([]) == []

    def test_all_none_pocs(self):
        profiles = [_snapshot("W1", poc=None), _snapshot("W2", poc=None)]
        assert build_vpoc_series(profiles) == []

    def test_preserves_label_and_timestamp(self):
        profiles = [_snapshot("Week-1", poc=50000, timestamp=1000)]
        series = build_vpoc_series(profiles)
        assert series[0]["label"] == "Week-1"
        assert series[0]["timestamp"] == 1000


# ── Trend Classification ─────────────────────────────────────────────────────

class TestClassifyVpocTrend:

    def test_insufficient_data(self):
        """Less than min_weeks profiles → insufficient_data."""
        profiles = _make_rising_weeks(2)
        result = classify_vpoc_trend(profiles, min_weeks=3)
        assert result["direction"] == "insufficient_data"
        assert result["weeks_analyzed"] == 2

    def test_rising_trend(self):
        """Steadily rising POC → rising."""
        profiles = _make_rising_weeks(6, step=200)
        result = classify_vpoc_trend(profiles, min_weeks=3)
        assert result["direction"] == "rising"
        assert result["strength"] >= 0.8  # very consistent
        assert result["slope"] > 0

    def test_falling_trend(self):
        """Steadily falling POC → falling."""
        profiles = _make_falling_weeks(6, step=200)
        result = classify_vpoc_trend(profiles, min_weeks=3)
        assert result["direction"] == "falling"
        assert result["slope"] < 0

    def test_flattening_low_slope(self):
        """Nearly flat POC → flattening."""
        profiles = _make_flat_weeks(6, poc=50000, noise=5)
        result = classify_vpoc_trend(profiles, min_weeks=3)
        assert result["direction"] == "flattening"

    def test_choppy_but_net_up_is_flattening(self):
        """Choppy POC (many direction changes) even if net slope positive → flattening."""
        profiles = [
            _snapshot("W1", poc=50000),
            _snapshot("W2", poc=50200),  # up
            _snapshot("W3", poc=50100),  # down
            _snapshot("W4", poc=50300),  # up
            _snapshot("W5", poc=50200),  # down
            _snapshot("W6", poc=50400),  # up
        ]
        result = classify_vpoc_trend(profiles, min_weeks=3, trend_consistency=0.70)
        # 5 moves: up, down, up, down, up → 3 up, 2 down → 3/5=0.60 < 0.70
        assert result["direction"] == "flattening"

    def test_mostly_up_with_few_reversals_is_rising(self):
        """Mostly up with few reversals → rising."""
        profiles = [
            _snapshot("W1", poc=50000),
            _snapshot("W2", poc=50200),  # up
            _snapshot("W3", poc=50400),  # up
            _snapshot("W4", poc=50300),  # down (1 reversal)
            _snapshot("W5", poc=50600),  # up
            _snapshot("W6", poc=50800),  # up
        ]
        result = classify_vpoc_trend(profiles, min_weeks=3, trend_consistency=0.60)
        # 5 moves: up, up, down, up, up → 4 up, 1 down → 4/5=0.80 >= 0.60
        assert result["direction"] == "rising"

    def test_slope_calculation(self):
        """Linear regression slope should approximate the step size."""
        profiles = _make_rising_weeks(6, step=100)
        result = classify_vpoc_trend(profiles, min_weeks=3)
        # Slope should be close to 100 $/week
        assert abs(result["slope"] - 100.0) < 5.0

    def test_direction_changes_count(self):
        """direction_changes counts reversals (not including first/last)."""
        profiles = [
            _snapshot("W1", poc=50000),
            _snapshot("W2", poc=50100),  # up
            _snapshot("W3", poc=50000),  # down (change 1)
            _snapshot("W4", poc=50100),  # up   (change 2)
            _snapshot("W5", poc=50000),  # down (change 3)
        ]
        result = classify_vpoc_trend(profiles, min_weeks=3)
        assert result["direction_changes"] == 3

    def test_consecutive_weeks_up(self):
        """consecutive_weeks counts most recent unbroken direction."""
        profiles = _make_rising_weeks(6, step=100)
        result = classify_vpoc_trend(profiles, min_weeks=3)
        assert result["consecutive_weeks"] == 5  # all 5 moves up

    def test_consecutive_weeks_broken(self):
        """consecutive_weeks resets on direction change."""
        profiles = [
            _snapshot("W1", poc=50000),
            _snapshot("W2", poc=50100),  # up
            _snapshot("W3", poc=50200),  # up
            _snapshot("W4", poc=50100),  # down → breaks streak
            _snapshot("W5", poc=50000),  # down
            _snapshot("W6", poc=49900),  # down
        ]
        result = classify_vpoc_trend(profiles, min_weeks=3)
        assert result["consecutive_weeks"] == 3  # last 3 moves down

    def test_avg_price_calculation(self):
        profiles = _make_rising_weeks(5, start_poc=50000, step=100)
        result = classify_vpoc_trend(profiles, min_weeks=3)
        assert result["avg_price"] == pytest.approx(50200.0)

    def test_slope_pct_calculation(self):
        """slope_pct = slope / avg_price."""
        profiles = _make_rising_weeks(5, start_poc=50000, step=100)
        result = classify_vpoc_trend(profiles, min_weeks=3)
        expected_pct = 100.0 / 50200.0
        assert result["slope_pct"] == pytest.approx(expected_pct, abs=1e-6)

    def test_message_contains_key_info(self):
        profiles = _make_rising_weeks(6, step=200)
        result = classify_vpoc_trend(profiles, min_weeks=3)
        assert "rising" in result["message"]
        assert "slope=" in result["message"]


if __name__ == "__main__":
    # Repo convention is `python tests/test_X.py` — without this guard,
    # running this pytest-class file that way silently executes zero tests.
    sys.exit(pytest.main([__file__, "-q"]))
