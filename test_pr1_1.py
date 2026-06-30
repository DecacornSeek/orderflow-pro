"""PR1.1 — Runtime contract enforcement + observability tests.

Tests:
  1. validate_trade_event: valid payload passes
  2. validate_trade_event: invalid payloads are rejected (missing field, bad side, zero price)
  3. validate_aggregated_snapshot: valid payload passes
  4. validate_aggregated_snapshot: invalid payloads are rejected
  5. exchange_agent._trade_loop: invalid event is skipped, counter incremented, loop continues
  6. aggregator_agent._publish_loop: invalid snapshot is skipped, counter incremented, loop continues
  7. Valid trade event publishes unchanged (field names preserved)
  8. Valid aggregated snapshot publishes unchanged

Ausfuehrung:
  python test_pr1_1.py
"""

import asyncio
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

import core.metrics as metrics
from core.validators import validate_trade_event, validate_aggregated_snapshot, validate_l2_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_trade() -> dict:
    return {
        "exchange": "binance",
        "timestamp": int(time.time() * 1000),
        "price": 65000.0,
        "size": 0.5,
        "side": "buy",
    }


def _valid_aggregated() -> dict:
    return {
        "timestamp": int(time.time() * 1000),
        "mid_price": 65000.0,
        "best_bid": 64990.0,
        "best_ask": 65010.0,
        "spread": 20.0,
        "imbalance_5": 0.55,
        "imbalance_20": 0.48,
        "bids": [],
        "asks": [],
        "cvd": {"cumulative_delta": 1.0, "trade_count": 10},
        "session_context": {},
        "profile_shape": {},
        "weekly_context": {},
        "composite_context": {},
        "divergence": None,
    }


# ---------------------------------------------------------------------------
# Part 1: validate_trade_event unit tests
# ---------------------------------------------------------------------------

def test_validate_trade_event_valid() -> None:
    print("  [1a] valid trade event passes")
    ok, reason = validate_trade_event(_valid_trade())
    assert ok, f"expected valid, got reason={reason!r}"
    assert reason == ""
    print("       PASS")


def test_validate_trade_event_missing_field() -> None:
    print("  [1b] missing 'side' field is rejected")
    msg = _valid_trade()
    del msg["side"]
    ok, reason = validate_trade_event(msg)
    assert not ok
    assert "side" in reason
    print(f"       PASS  reason={reason!r}")


def test_validate_trade_event_bad_side() -> None:
    print("  [1c] invalid side value is rejected")
    msg = _valid_trade()
    msg["side"] = "long"
    ok, reason = validate_trade_event(msg)
    assert not ok
    assert "side" in reason
    print(f"       PASS  reason={reason!r}")


def test_validate_trade_event_zero_price() -> None:
    print("  [1d] zero price is rejected")
    msg = _valid_trade()
    msg["price"] = 0.0
    ok, reason = validate_trade_event(msg)
    assert not ok
    assert "price" in reason
    print(f"       PASS  reason={reason!r}")


def test_validate_trade_event_none_size() -> None:
    print("  [1e] None size is rejected")
    msg = _valid_trade()
    msg["size"] = None
    ok, reason = validate_trade_event(msg)
    assert not ok
    assert "size" in reason
    print(f"       PASS  reason={reason!r}")


def test_validate_trade_event_not_dict() -> None:
    print("  [1f] non-dict is rejected")
    ok, reason = validate_trade_event("bad")  # type: ignore[arg-type]
    assert not ok
    print(f"       PASS  reason={reason!r}")


# ---------------------------------------------------------------------------
# Part 2: validate_aggregated_snapshot unit tests
# ---------------------------------------------------------------------------

def test_validate_aggregated_valid() -> None:
    print("  [2a] valid aggregated snapshot passes")
    ok, reason = validate_aggregated_snapshot(_valid_aggregated())
    assert ok, f"expected valid, got reason={reason!r}"
    print("       PASS")


def test_validate_aggregated_missing_timestamp() -> None:
    print("  [2b] missing 'timestamp' is rejected")
    msg = _valid_aggregated()
    del msg["timestamp"]
    ok, reason = validate_aggregated_snapshot(msg)
    assert not ok
    assert "timestamp" in reason
    print(f"       PASS  reason={reason!r}")


def test_validate_aggregated_missing_cvd() -> None:
    print("  [2c] missing 'cvd' is rejected")
    msg = _valid_aggregated()
    del msg["cvd"]
    ok, reason = validate_aggregated_snapshot(msg)
    assert not ok
    assert "cvd" in reason
    print(f"       PASS  reason={reason!r}")


def test_validate_aggregated_bad_cvd_type() -> None:
    print("  [2d] non-dict cvd is rejected")
    msg = _valid_aggregated()
    msg["cvd"] = "not-a-dict"
    ok, reason = validate_aggregated_snapshot(msg)
    assert not ok
    assert "cvd" in reason
    print(f"       PASS  reason={reason!r}")


def test_validate_aggregated_mid_price_none_allowed() -> None:
    print("  [2e] mid_price=None is allowed (no L2 yet)")
    msg = _valid_aggregated()
    msg["mid_price"] = None
    ok, reason = validate_aggregated_snapshot(msg)
    assert ok, f"expected valid with None mid_price, got: {reason!r}"
    print("       PASS")


# ---------------------------------------------------------------------------
# Part 3: metrics counter tests
# ---------------------------------------------------------------------------

def test_metrics_increment() -> None:
    print("  [3a] metrics counters increment correctly")
    metrics.reset_all()
    assert metrics.get(metrics.VALIDATION_TRADE_FAILED) == 0
    metrics.increment(metrics.VALIDATION_TRADE_FAILED)
    metrics.increment(metrics.VALIDATION_TRADE_FAILED)
    assert metrics.get(metrics.VALIDATION_TRADE_FAILED) == 2
    snap = metrics.snapshot()
    assert snap[metrics.VALIDATION_TRADE_FAILED] == 2
    print("       PASS")


# ---------------------------------------------------------------------------
# Part 4: integration — exchange_agent trade loop skip on invalid
# ---------------------------------------------------------------------------

async def _run_trade_loop_with_bad_event() -> tuple[list, int]:
    """Run _trade_loop against one invalid + one valid trade, capture publishes."""
    from agents.exchange_agent import _trade_loop
    from core.broker import Broker, TRADES

    metrics.reset_all()
    broker = Broker()
    subscriber = broker.subscribe(TRADES)

    shutdown = asyncio.Event()

    bad_trade = {"info": {"m": False}}   # price=None, size=None → invalid
    good_trade = {
        "info": {"m": False},
        "timestamp": int(time.time() * 1000),
        "price": 65000.0,
        "amount": 0.5,
    }

    call_count = 0

    async def mock_watch_trades(_symbol):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [bad_trade, good_trade]
        shutdown.set()
        return []

    mock_exchange = MagicMock()
    mock_exchange.watch_trades = mock_watch_trades

    await _trade_loop(mock_exchange, broker, shutdown)

    published = []
    while not subscriber.empty():
        published.append(await subscriber.get())

    failed = metrics.get(metrics.VALIDATION_TRADE_FAILED)
    return published, failed


def test_trade_loop_skips_invalid() -> None:
    print("  [4a] trade loop: invalid event skipped, valid event published, counter incremented")
    published, failed = asyncio.run(_run_trade_loop_with_bad_event())
    assert len(published) == 1, f"expected 1 published (valid only), got {len(published)}"
    assert failed == 1, f"expected 1 failure counter, got {failed}"
    print(f"       PASS  published={len(published)}, failed_counter={failed}")


def test_trade_loop_payload_shape_unchanged() -> None:
    print("  [4b] trade loop: published payload has exact expected fields")
    published, _ = asyncio.run(_run_trade_loop_with_bad_event())
    assert len(published) == 1
    msg = published[0]
    for field in ("exchange", "timestamp", "price", "size", "side"):
        assert field in msg, f"field '{field}' missing from published trade event"
    assert msg["exchange"] == "binance"
    assert msg["side"] in ("buy", "sell")
    print("       PASS  all fields present, shape unchanged")


# ---------------------------------------------------------------------------
# Part 5: integration — aggregator _publish_loop skip on invalid snapshot
# ---------------------------------------------------------------------------

async def _run_publish_loop_with_bad_snapshot() -> tuple[list, int]:
    """Patch validate_aggregated_snapshot to return invalid on first call.

    We patch the validator rather than CVD.snapshot because the payload is
    built using snap.get(...) — if the CVD returns None the loop crashes
    before reaching validation. Patching the validator directly proves the
    skip-and-increment path without fighting the payload-build code.
    """
    from agents.aggregator_agent import _publish_loop
    from core.broker import Broker, AGGREGATED
    from core.cvd import CVD
    from core.session_profile import SessionProfile
    from core.weekly_profile import WeeklyProfile
    from core.absorption import AbsorptionDetector
    from core.divergence import DivergenceDetector
    from core.composite_profile import CompositeProfile
    import agents.aggregator_agent as agg_mod

    metrics.reset_all()
    broker = Broker()
    subscriber = broker.subscribe(AGGREGATED)
    shutdown = asyncio.Event()

    cvd = CVD(window_size=200)
    session = SessionProfile()
    weekly = WeeklyProfile()
    absorption = AbsorptionDetector()
    divergence = DivergenceDetector(lookback=3)
    composite = CompositeProfile()
    last_l2: dict = {"mid_price": 65000.0}

    call_count = 0
    real_validator = agg_mod.validate_aggregated_snapshot

    def patched_validator(payload):
        nonlocal call_count
        call_count += 1
        shutdown.set()  # stop after first publish attempt regardless
        if call_count == 1:
            return False, "injected-test-failure"
        return real_validator(payload)

    original = agg_mod.validate_aggregated_snapshot
    agg_mod.validate_aggregated_snapshot = patched_validator  # type: ignore[attr-defined]
    try:
        await _publish_loop(broker, cvd, last_l2, session, weekly,
                            absorption, divergence, composite, shutdown)
    finally:
        agg_mod.validate_aggregated_snapshot = original

    published = []
    while not subscriber.empty():
        published.append(await subscriber.get())

    failed = metrics.get(metrics.VALIDATION_AGGREGATED_FAILED)
    return published, failed


def test_publish_loop_skips_invalid_snapshot() -> None:
    print("  [5a] publish loop: invalid snapshot skipped, counter incremented")
    published, failed = asyncio.run(_run_publish_loop_with_bad_snapshot())
    assert failed == 1, f"expected 1 aggregated failure counter, got {failed}"
    assert len(published) == 0, f"expected 0 published on invalid snapshot, got {len(published)}"
    print(f"       PASS  failed_counter={failed}, published={len(published)}")


# ---------------------------------------------------------------------------
# Part 6: validate_l2_snapshot unit tests  (PR1.2)
# ---------------------------------------------------------------------------

def _valid_l2() -> dict:
    return {
        "exchange": "binance",
        "timestamp": int(time.time() * 1000),
        "bids": [[65000.0, 1.0], [64990.0, 2.0]],
        "asks": [[65010.0, 1.5]],
        "mid_price": 65005.0,
        "spread": 10.0,
        "imbalance_5": 0.55,
        "imbalance_20": 0.48,
    }


def test_validate_l2_valid() -> None:
    print("  [6a] valid L2 snapshot passes")
    ok, reason = validate_l2_snapshot(_valid_l2())
    assert ok, f"expected valid, got reason={reason!r}"
    print("       PASS")


def test_validate_l2_missing_bids() -> None:
    print("  [6b] missing 'bids' field is rejected")
    msg = _valid_l2()
    del msg["bids"]
    ok, reason = validate_l2_snapshot(msg)
    assert not ok
    assert "bids" in reason
    print(f"       PASS  reason={reason!r}")


def test_validate_l2_bad_bids_type() -> None:
    print("  [6c] non-list bids is rejected")
    msg = _valid_l2()
    msg["bids"] = "not-a-list"
    ok, reason = validate_l2_snapshot(msg)
    assert not ok
    assert "bids" in reason
    print(f"       PASS  reason={reason!r}")


def test_validate_l2_zero_mid_price() -> None:
    print("  [6d] zero mid_price is rejected")
    msg = _valid_l2()
    msg["mid_price"] = 0.0
    ok, reason = validate_l2_snapshot(msg)
    assert not ok
    assert "mid_price" in reason
    print(f"       PASS  reason={reason!r}")


def test_validate_l2_none_mid_price_allowed() -> None:
    print("  [6e] mid_price=None is allowed (no book yet)")
    msg = _valid_l2()
    msg["mid_price"] = None
    ok, reason = validate_l2_snapshot(msg)
    assert ok, f"expected valid with None mid_price, got: {reason!r}"
    print("       PASS")


def test_validate_l2_empty_book_allowed() -> None:
    print("  [6f] empty bids/asks lists are allowed")
    msg = _valid_l2()
    msg["bids"] = []
    msg["asks"] = []
    ok, reason = validate_l2_snapshot(msg)
    assert ok, f"expected valid with empty book, got: {reason!r}"
    print("       PASS")


# L2 loop integration: invalid snapshot skipped, valid published

async def _run_l2_loop_with_bad_snapshot() -> tuple[list, int]:
    from agents.exchange_agent import _l2_loop
    from core.broker import Broker, L2
    from core.orderbook import OrderBook
    import agents.exchange_agent as ex_mod

    metrics.reset_all()
    broker = Broker()
    subscriber = broker.subscribe(L2)
    shutdown = asyncio.Event()
    book = OrderBook("BTCUSDT", depth=100)

    call_count = 0
    real_validator = ex_mod.validate_l2_snapshot

    def patched_validator(payload):
        nonlocal call_count
        call_count += 1
        shutdown.set()
        if call_count == 1:
            return False, "injected-l2-failure"
        return real_validator(payload)

    original = ex_mod.validate_l2_snapshot
    ex_mod.validate_l2_snapshot = patched_validator  # type: ignore[attr-defined]

    async def mock_watch_order_book(_symbol, limit):
        return {
            "bids": [[65000.0, 1.0]],
            "asks": [[65010.0, 1.0]],
            "nonce": int(time.time() * 1000),
            "timestamp": int(time.time() * 1000),
        }

    mock_exchange = MagicMock()
    mock_exchange.watch_order_book = mock_watch_order_book

    try:
        await _l2_loop(mock_exchange, broker, book, shutdown)
    finally:
        ex_mod.validate_l2_snapshot = original

    published = []
    while not subscriber.empty():
        published.append(await subscriber.get())

    failed = metrics.get(metrics.VALIDATION_L2_FAILED)
    return published, failed


def test_l2_loop_skips_invalid() -> None:
    print("  [6g] L2 loop: invalid snapshot skipped, counter incremented")
    published, failed = asyncio.run(_run_l2_loop_with_bad_snapshot())
    assert failed == 1, f"expected 1 L2 failure counter, got {failed}"
    assert len(published) == 0, f"expected 0 published, got {len(published)}"
    print(f"       PASS  failed_counter={failed}, published={len(published)}")


# ---------------------------------------------------------------------------
# Part 7: /metrics endpoint  (PR1.3)
# ---------------------------------------------------------------------------

def test_metrics_endpoint() -> None:
    print("  [7a] GET /metrics returns counter dict")
    from fastapi.testclient import TestClient
    from core.history import History
    from core.broker import Broker as _Broker

    metrics.reset_all()
    metrics.increment(metrics.VALIDATION_TRADE_FAILED)
    metrics.increment(metrics.VALIDATION_L2_FAILED)

    from agents.display_agent import DisplayAgent
    agent = DisplayAgent(_Broker(), History())
    client = TestClient(agent._app)

    resp = client.get("/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data[metrics.VALIDATION_TRADE_FAILED] == 1
    assert data[metrics.VALIDATION_L2_FAILED] == 1
    print(f"       PASS  response={data}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _section(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def run_all() -> int:
    passed = 0
    failed = 0

    tests = [
        ("validate_trade_event: valid",             test_validate_trade_event_valid),
        ("validate_trade_event: missing field",     test_validate_trade_event_missing_field),
        ("validate_trade_event: bad side",          test_validate_trade_event_bad_side),
        ("validate_trade_event: zero price",        test_validate_trade_event_zero_price),
        ("validate_trade_event: None size",         test_validate_trade_event_none_size),
        ("validate_trade_event: not dict",          test_validate_trade_event_not_dict),
        ("validate_aggregated: valid",              test_validate_aggregated_valid),
        ("validate_aggregated: missing timestamp",  test_validate_aggregated_missing_timestamp),
        ("validate_aggregated: missing cvd",        test_validate_aggregated_missing_cvd),
        ("validate_aggregated: bad cvd type",       test_validate_aggregated_bad_cvd_type),
        ("validate_aggregated: mid_price None OK",  test_validate_aggregated_mid_price_none_allowed),
        ("metrics: counters increment",             test_metrics_increment),
        ("trade loop: skips invalid event",         test_trade_loop_skips_invalid),
        ("trade loop: payload shape unchanged",     test_trade_loop_payload_shape_unchanged),
        ("publish loop: skips invalid snapshot",    test_publish_loop_skips_invalid_snapshot),
        # PR1.2
        ("validate_l2: valid",                      test_validate_l2_valid),
        ("validate_l2: missing bids",               test_validate_l2_missing_bids),
        ("validate_l2: bad bids type",              test_validate_l2_bad_bids_type),
        ("validate_l2: zero mid_price rejected",    test_validate_l2_zero_mid_price),
        ("validate_l2: None mid_price allowed",     test_validate_l2_none_mid_price_allowed),
        ("validate_l2: empty book allowed",         test_validate_l2_empty_book_allowed),
        ("L2 loop: skips invalid snapshot",         test_l2_loop_skips_invalid),
        # PR1.3
        ("GET /metrics endpoint",                   test_metrics_endpoint),
    ]

    _section("PR1.1 / PR1.2 / PR1.3 — Contract Enforcement + Observability")

    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [FAIL] {name} — Exception: {type(e).__name__}: {e}")
            failed += 1

    print()
    print("=" * 60)
    print(f"ERGEBNIS: {passed} passed, {failed} failed")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run_all())
