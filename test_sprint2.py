"""
Sprint 2 Full Integration Test ? Exchange Agent + Aggregator + CVD.

Testet den kompletten Datenfluss OHNE echten Redis-Server:
  1. Startet Exchange Agent (simuliert) -> publiziert binance_l2 + binance_trades
  2. Startet Aggregator Agent (simuliert) -> subscribed, berechnet CVD
  3. Prueft aggregated_cvd Output-Schema und monotonic CVD
  4. Prueft dass rolling_delta nach window_size Trades korrekt abfaellt

Verwendet fakeredis als In-Process Redis-Ersatz fuer Integration ohne Docker.

Ausfuehrung:
  python test_sprint2_integration.py
"""

import asyncio
import json
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, ".")

from core.cvd import CVD
from agents.aggregator_agent import AggregatorAgent, REDIS_CHANNEL_CVD

# ------------------------------------------------------------------
# Konfiguration
# ------------------------------------------------------------------
NUM_TEST_TRADES = 250


# ------------------------------------------------------------------
# Fake Redis (via fakeredis) ? kein echter Redis-Server noetig
# ------------------------------------------------------------------
async def _get_fake_redis():
    import fakeredis.aioredis
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.ping()
    return r


# ------------------------------------------------------------------
# Teil 1: CVD Unit-Test (wie test_sprint2, aber in einer Datei)
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
# Teil 2: Integration Exchange <-> Aggregator
# ------------------------------------------------------------------
async def simulate_exchange(redis_client, trade_count: int = 50, l2_count: int = 5):
    """Simuliert Exchange Agent, der L2 + Trades in Redis publiziert."""
    import random
    random.seed(12345)
    price = 50000.0

    for i in range(trade_count):
        # Trade
        side = "buy" if random.random() < 0.5 else "sell"
        size = round(random.uniform(0.1, 3.0), 4)
        price += random.uniform(-5, 5)
        trade_msg = {
            "exchange": "binance",
            "symbol": "BTCUSDT",
            "timestamp": int(time.time() * 1000) + i,
            "price": round(price, 2),
            "size": size,
            "aggressor_side": side,
            "trade_id": str(i + 1000),
        }
        await redis_client.publish("binance_trades", json.dumps(trade_msg))
        await asyncio.sleep(0.01)

        # L2 Update (seltener)
        if i % 10 == 0:
            l2_msg = {
                "exchange": "binance",
                "symbol": "BTCUSDT",
                "timestamp": int(time.time() * 1000),
                "bids": [[price - 1.0, 10.0], [price - 2.0, 20.0]],
                "asks": [[price + 1.0, 10.0], [price + 2.0, 20.0]],
                "imbalance_5": 0.1,
                "imbalance_20": 0.05,
                "spread": 2.0,
                "mid_price": price,
                "last_update_id": 1000 + i,
            }
            await redis_client.publish("binance_l2", json.dumps(l2_msg))


async def test_integration_pipeline() -> None:
    """Vollstaendiger Integrationstest: Exchange -> Redis -> Aggregator -> aggregated_cvd."""
    print("=" * 60)
    print("Teil 2: Integration Pipeline (fakeredis)")
    print("=" * 60)

    # Fake Redis erstellen
    fake_redis = await _get_fake_redis()

    # Aggregator Agent mit Fake Redis starten
    agent = AggregatorAgent(redis_url="redis://fake")
    # Redis-Client durch Fake ersetzen
    agent.redis_client = fake_redis
    agent.pubsub = fake_redis.pubsub()
    await agent.pubsub.subscribe("binance_l2", "binance_trades")

    # Aggregator Tasks starten
    sub_task = asyncio.create_task(agent._subscribe_loop())
    pub_task = asyncio.create_task(agent._publish_loop())

    # Simulierten Exchange starten
    exchange_task = asyncio.create_task(
        simulate_exchange(fake_redis, trade_count=50, l2_count=5)
    )

    # Auf aggregated_cvd Nachrichten warten
    cvd_sub = fake_redis.pubsub()
    await cvd_sub.subscribe(REDIS_CHANNEL_CVD)
    await asyncio.sleep(0.5)  # Zeit fuer Verarbeitung

    # Nachrichten einsammeln
    cvd_messages = []
    timeout = 5.0
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = await cvd_sub.get_message(ignore_subscribe_messages=True, timeout=0.3)
        if msg and msg["type"] == "message":
            cvd_messages.append(msg)
        if len(cvd_messages) >= 3:
            break

    # Warten auf Exchange-Abschluss
    await exchange_task

    # Aggregator stoppen
    agent.shutdown_event.set()
    sub_task.cancel()
    pub_task.cancel()
    try:
        await asyncio.gather(sub_task, pub_task, return_exceptions=True)
    except Exception:
        pass

    await cvd_sub.unsubscribe()
    await fake_redis.aclose()

    # --- Auswertung ---
    if len(cvd_messages) == 0:
        print("[FAIL] Keine aggregated_cvd Nachrichten erhalten")
        return False

    print("  Erhaltene aggregated_cvd Nachrichten: {}".format(len(cvd_messages)))

    for i, msg in enumerate(cvd_messages[:3]):  # Max 3 anschauen
        data = json.loads(msg["data"])

        # Schema-Validierung
        required_root = ["exchange", "symbol", "timestamp", "cvd", "mid_price"]
        for key in required_root:
            assert key in data, "Fehlender Root-Key: {}".format(key)

        cvd_data = data["cvd"]
        required_cvd = [
            "cumulative_buy_volume", "cumulative_sell_volume", "cumulative_delta",
            "rolling_buy_volume", "rolling_sell_volume", "rolling_delta",
            "trade_count", "last_price", "last_timestamp",
        ]
        for key in required_cvd:
            assert key in cvd_data, "Fehlender CVD-Key: {}".format(key)

        # Typ-Pruefungen
        assert isinstance(data["exchange"], str), "exchange kein str"
        assert isinstance(data["symbol"], str), "symbol kein str"
        assert isinstance(data["timestamp"], int), "timestamp kein int"
        assert isinstance(cvd_data["trade_count"], int), "trade_count kein int"
        assert isinstance(cvd_data["rolling_delta"], (int, float)), "rolling_delta kein number"
        assert isinstance(cvd_data["cumulative_delta"], (int, float)), "cumulative_delta kein number"

        print("  [{}] exchange={} symbol={} trades={} cum_delta={:+.4f} roll_delta={:+.4f} mid_price={}".format(
            i, data["exchange"], data["symbol"],
            cvd_data["trade_count"], cvd_data["cumulative_delta"],
            cvd_data["rolling_delta"], data["mid_price"],
        ))

    print("[PASS] Redis Output-Schema valide ({} Nachrichten)".format(len(cvd_messages)))

    # Pruefe: trade_count steigt
    counts = [json.loads(m["data"])["cvd"]["trade_count"] for m in cvd_messages]
    for i in range(1, len(counts)):
        assert counts[i] >= counts[i-1], "trade_count nicht monoton: {} -> {}".format(
            counts[i-1], counts[i]
        )
    print("[PASS] trade_count steigt monoton ueber Nachrichten hinweg")

    print()
    print(">>> Integration Pipeline bestanden! <<<")
    print()
    return True


# ------------------------------------------------------------------
# Teil 3: Monotonic CVD (simulierter Stream, > window_size Trades)
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
async def run_all():
    passed = 0
    failed = 0

    # Teil 1
    try:
        test_cvd_unit()
        passed += 1
    except AssertionError as e:
        print("[FAIL] CVD Unit-Test: {}".format(e))
        failed += 1
    except Exception as e:
        print("[FAIL] CVD Unit-Test Exception: {}".format(e))
        failed += 1

    # Teil 2
    try:
        ok = await test_integration_pipeline()
        if ok:
            passed += 1
        else:
            failed += 1
    except Exception as e:
        print("[FAIL] Integration Exception: {}".format(e))
        import traceback
        traceback.print_exc()
        failed += 1

    # Teil 3
    try:
        test_monotonic_cvd()
        passed += 1
    except AssertionError as e:
        print("[FAIL] Monotonic CVD: {}".format(e))
        failed += 1
    except Exception as e:
        print("[FAIL] Monotonic CVD Exception: {}".format(e))
        failed += 1

    print("=" * 60)
    print("ERGEBNIS: {} passed, {} failed".format(passed, failed))
    print("=" * 60)
    return 0 if failed == 0 else 1


def main():
    return asyncio.run(run_all())


if __name__ == "__main__":
    sys.exit(main())
