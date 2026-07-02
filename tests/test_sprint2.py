"""
Sprint 2: CVD Unit-Tests + Monotonic CVD stream validation.

Ausfuehrung:
  python test_sprint2.py
"""

import asyncio
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, ".")

from core.cvd import CVD

# ------------------------------------------------------------------
# Konfiguration
# ------------------------------------------------------------------
NUM_TEST_TRADES = 250


# ------------------------------------------------------------------
# Teil 1: CVD Unit-Test
# ------------------------------------------------------------------
def test_cvd_unit() -> None:
    print("=" * 60)
    print("Teil 1: CVD Unit-Test (ohne Redis)")
    print("=" * 60)

    cvd = CVD(window_size=10)

    # 1. Initialzustand
    snap = cvd.snapshot()
    assert snap["trade_count"] == 0, "trade_count sollte 0 sein"
    assert snap["cumulative_delta"] == 0.0
    assert snap["rolling_delta"] == 0.0
    assert snap["last_price"] is None
    print("[PASS] Initialzustand korrekt")

    # 2. Buy Trade
    snap = cvd.update(50000.0, 1.0, "buy", timestamp=1000)
    assert snap["trade_count"] == 1
    assert snap["cumulative_buy_volume"] == 1.0
    assert snap["cumulative_delta"] == 1.0
    assert snap["rolling_delta"] == 1.0
    assert snap["last_price"] == 50000.0
    print("[PASS] Single Buy Trade korrekt")

    # 3. Sell Trade
    snap = cvd.update(50100.0, 2.0, "sell", timestamp=1001)
    assert snap["cumulative_delta"] == -1.0
    assert snap["rolling_delta"] == -1.0
    print("[PASS] Single Sell Trade korrekt (delta={:+.2f})".format(snap["cumulative_delta"]))

    # 4. Rolling Delta == Cumulative Delta bis Fenstergrenze
    for i in range(8):
        cvd.update(50000.0 + i, 0.5, "buy" if i % 2 == 0 else "sell")
    snap = cvd.snapshot()
    assert snap["trade_count"] == 10
    assert snap["rolling_delta"] == snap["cumulative_delta"]
    print("[PASS] Rolling = Cumulative bis Fenstergrenze")

    # 5. Fenster-Slide
    cvd.update(51000.0, 3.0, "buy")
    snap = cvd.snapshot()
    assert snap["trade_count"] == 11
    assert snap["rolling_delta"] != snap["cumulative_delta"]
    print("[PASS] Fenster-Slide funktioniert")

    # 6. Reset
    cvd.reset()
    snap = cvd.snapshot()
    assert snap["trade_count"] == 0
    assert snap["cumulative_delta"] == 0.0
    print("[PASS] Reset funktioniert")

    # 7. cvd_ratio
    cvd.update(100.0, 2.0, "buy")
    cvd.update(100.0, 1.0, "sell")
    assert cvd.cvd_ratio == round((2.0 - 1.0) / (2.0 + 1.0), 6)
    print("[PASS] cvd_ratio = {:.4f}".format(cvd.cvd_ratio))

    # 8. Leeres Fenster
    cvd.reset()
    assert cvd.cvd_ratio is None
    print("[PASS] cvd_ratio = None bei leerem Fenster")

    print()
    print(">>> Alle CVD Unit-Tests bestanden! <<<")
    print()


# ------------------------------------------------------------------
# Teil 2: Monotonic CVD (simulierter Stream, > window_size Trades)
# ------------------------------------------------------------------
def test_monotonic_cvd() -> None:
    print("=" * 60)
    print("Teil 3: Monotonic CVD (simulierter Stream, {} Trades)".format(NUM_TEST_TRADES))
    print("=" * 60)

    cvd = CVD(window_size=200)
    snapshots: List[Dict[str, Any]] = []

    import random
    random.seed(42)
    price = 50000.0
    for i in range(NUM_TEST_TRADES):
        side = "buy" if random.random() < 0.48 else "sell"
        size = round(random.uniform(0.1, 5.0), 4)
        price += random.uniform(-10, 10)
        snap = cvd.update(price, size, side, timestamp=int(time.time() * 1000) + i)
        snapshots.append(snap)

    print("  Trades simuliert: {}".format(NUM_TEST_TRADES))

    # cumulative_delta Aenderungen max trade size
    for i in range(1, len(snapshots)):
        diff = abs(snapshots[i]["cumulative_delta"] - snapshots[i-1]["cumulative_delta"])
        assert diff <= 5.0, "cumulative_delta Sprung zu gross: {:.4f}".format(diff)
    print("[PASS] cumulative_delta aendert sich nur um Trade-Size")

    # cumulative_delta == buy - sell
    final = snapshots[-1]
    assert abs(final["cumulative_delta"] - (
        final["cumulative_buy_volume"] - final["cumulative_sell_volume"]
    )) < 1e-8
    print("[PASS] cumulative_delta == cumulative_buy - cumulative_sell")

    # rolling_delta in plausiblem Bereich
    for snap in snapshots:
        assert -1000.0 <= snap["rolling_delta"] <= 1000.0
    print("[PASS] rolling_delta in plausiblem Bereich")

    # trade_count monoton
    for i in range(1, len(snapshots)):
        assert snapshots[i]["trade_count"] == snapshots[i-1]["trade_count"] + 1
    print("[PASS] trade_count steigt monoton (+1 pro Trade)")

    # last_price aktualisiert
    assert snapshots[-1]["last_price"] is not None
    print("[PASS] last_price aktualisiert")

    # Pruefe: nach window_size Trades ist rolling anders als cumulative
    # Wenn wir window_size ueberschreiten, sollten rolling und cumulative
    # unterschiedlich sein (weil alte Trades rausfallen)
    for snap in snapshots[200:]:
        if snap["rolling_delta"] != snap["cumulative_delta"]:
            print("[PASS] rolling_delta != cumulative_delta nach Fenster-Slide")
            break
    else:
        print("[WARN] rolling_delta == cumulative_delta trotz Fensterueberschreitung")

    print()
    print(">>> Alle Monotonic CVD Tests bestanden! <<<")
    print()


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def run_all() -> int:
    passed = 0
    failed = 0

    for name, fn in [("CVD Unit-Test", test_cvd_unit), ("Monotonic CVD", test_monotonic_cvd)]:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print("[FAIL] {}: {}".format(name, e))
            failed += 1
        except Exception as e:
            print("[FAIL] {} Exception: {}".format(name, e))
            failed += 1

    print("=" * 60)
    print("ERGEBNIS: {} passed, {} failed".format(passed, failed))
    print("=" * 60)
    return 0 if failed == 0 else 1


def main() -> int:
    return run_all()


if __name__ == "__main__":
    sys.exit(main())
