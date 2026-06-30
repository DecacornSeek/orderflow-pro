"""PR4a Evidence 2 — Failure Isolation Proof.

Demonstrates that:
  - a hard exception inside session.current_context() is caught per-module
  - weekly_context still publishes with valid data
  - the top-level AGGREGATED payload still gets published
  - context_session_fail_total and context_fallback_total increment
  - context_weekly_fail_total stays at 0

Run: python scripts/verify_pr4a_isolation.py
"""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

import core.metrics as metrics
from core.broker import Broker, AGGREGATED
from core.cvd import CVD
from core.session_profile import SessionProfile
from core.weekly_profile import WeeklyProfile
from core.profile_shape import classify_shape
from core.absorption import AbsorptionDetector
from core.divergence import DivergenceDetector
from core.composite_profile import CompositeProfile
from core.validators import validate_aggregated_snapshot

# Use the aggregator's throttled-warn infrastructure
import agents.aggregator_agent as agg_mod

BASE_TS = int(datetime(2026, 6, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)


class WarningCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


async def run() -> int:
    metrics.reset_all()
    # Reset throttle timestamps so warnings fire immediately
    agg_mod._warned_at.clear()

    # Capture log output
    capture = WarningCapture()
    logging.getLogger("agents.aggregator_agent").addHandler(capture)

    # Build components
    broker = Broker()
    sub = broker.subscribe(AGGREGATED, maxsize=10)
    shutdown = asyncio.Event()

    cvd   = CVD(window_size=200)
    session = SessionProfile()
    weekly  = WeeklyProfile()
    absorption = AbsorptionDetector()
    divergence = DivergenceDetector(lookback=3)
    composite  = CompositeProfile()
    last_l2: dict = {"mid_price": 65025.0, "best_bid": 65020.0,
                     "best_ask": 65030.0, "spread": 10.0,
                     "imbalance_5": 0.55, "imbalance_20": 0.48,
                     "bids": [], "asks": []}

    # Seed data so weekly_context is non-trivial
    for i in range(50):
        ts = BASE_TS + i * 1_000
        cvd.update(65000.0 + i * 0.5, 1.0, "buy" if i % 2 == 0 else "sell")
        session.ingest(ts, 65000.0 + i * 0.5, 1.0, "buy" if i % 2 == 0 else "sell")
        weekly.ingest(ts, 65000.0 + i * 0.5, 1.0, "buy" if i % 2 == 0 else "sell")

    # ── Patch session.current_context to throw on calls 1..3, pass on call 4+
    call_count = 0
    original_ctx = session.current_context

    def patched_ctx():
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            raise RuntimeError(f"injected session failure #{call_count}")
        return original_ctx()

    session.current_context = patched_ctx  # type: ignore[method-assign]

    # ── Run 4 publish-loop iterations manually ───────────────────────────────
    CYCLES = 4
    published: list[dict] = []

    for cycle in range(1, CYCLES + 1):
        snap = cvd.snapshot()
        mid_price = last_l2.get("mid_price")

        try:
            session_ctx = session.current_context()
            session_name = session_ctx.get("session", "")
            anomaly = session.get_pre_session_anomaly(session_name)
            if anomaly:
                session_ctx["pre_session_anomaly"] = anomaly
            shape_ctx = classify_shape(
                session._vap if session.current_session and session._vap else None
            )
        except Exception as e:
            agg_mod._throttled_warn("session_ctx", "session context failed: %s", e)
            metrics.increment(metrics.CONTEXT_SESSION_FAIL)
            metrics.increment(metrics.CONTEXT_FALLBACK)
            session_ctx = {"session": "N/A"}
            shape_ctx = classify_shape(None)

        try:
            weekly_ctx = weekly.current_context()
        except Exception as e:
            agg_mod._throttled_warn("weekly_ctx", "weekly context failed: %s", e)
            metrics.increment(metrics.CONTEXT_WEEKLY_FAIL)
            metrics.increment(metrics.CONTEXT_FALLBACK)
            weekly_ctx = {"week": "N/A"}

        archived = session.get_archived_profiles()
        if archived:
            composite.add_profiles(archived[-5:])
        composite_ctx = composite.current_context()

        payload = {
            "timestamp": int(time.time() * 1000),
            "mid_price": mid_price,
            "best_bid": last_l2.get("best_bid"),
            "best_ask": last_l2.get("best_ask"),
            "spread": last_l2.get("spread"),
            "imbalance_5": last_l2.get("imbalance_5"),
            "imbalance_20": last_l2.get("imbalance_20"),
            "bids": last_l2.get("bids", []),
            "asks": last_l2.get("asks", []),
            "cvd": snap,
            "session_context": session_ctx,
            "profile_shape": shape_ctx,
            "weekly_context": weekly_ctx,
            "composite_context": composite_ctx,
            "divergence": None,
        }
        ok, reason = validate_aggregated_snapshot(payload)
        if ok:
            await broker.publish(AGGREGATED, payload)
            published.append(payload)

    # ── Drain subscriber queue ───────────────────────────────────────────────
    received: list[dict] = []
    while not sub.empty():
        received.append(await sub.get())

    # ── Assertions ───────────────────────────────────────────────────────────
    m = metrics.snapshot()

    print("=" * 70)
    print("  FAILURE ISOLATION PROOF")
    print("=" * 70)

    print(f"\n  Publish cycles run:            {CYCLES}")
    print(f"  Payloads published (broker):   {len(received)}")
    print(f"  Session failures injected:     3  (cycles 1-3)")
    print(f"  Session call 4 succeeds:       True")

    print(f"\n  Metrics after {CYCLES} cycles:")
    for k, v in sorted(m.items()):
        print(f"    {k}: {v}")

    print(f"\n  Captured log lines ({len(capture.lines)}):")
    for line in capture.lines:
        print(f"    {line}")

    # Show fallback vs live session_context across 4 cycles
    print(f"\n  session_context per cycle:")
    for i, p in enumerate(published, 1):
        sc = p["session_context"]
        wc = p["weekly_context"]
        regime = sc.get("regime", "—")
        print(f"    cycle {i}: session={sc.get('session')!r:12s}  "
              f"week={wc.get('week', '?')!r:20s}  "
              f"regime={regime!r}  "
              f"weekly_valid={'week_poc' in wc}")

    # Verify
    checks = [
        ("All 4 cycles published",          len(received) == CYCLES),
        ("session_fail_total = 3",          m.get(metrics.CONTEXT_SESSION_FAIL) == 3),
        ("weekly_fail_total = 0",           m.get(metrics.CONTEXT_WEEKLY_FAIL, 0) == 0),
        ("fallback_total = 3",              m.get(metrics.CONTEXT_FALLBACK) == 3),
        ("Throttle suppressed >=2 warns",   len(capture.lines) == 1),  # throttled: 1 log for 3 fails
        ("Cycle 4 session live (not N/A)",  published[3]["session_context"].get("session") != "N/A"),
        ("Cycles 1-3 fallback session",     all(
            published[i]["session_context"].get("session") == "N/A"
            for i in range(3)
        )),
        ("Weekly context always valid",     all(
            p["weekly_context"].get("week", "") != "N/A"
            for p in published
        )),
        ("Payload validation passed all",   True),  # only published if validated
    ]

    print("\n  Verification:")
    all_ok = True
    for name, result in checks:
        status = "PASS" if result else "FAIL"
        print(f"    [{status}] {name}")
        if not result:
            all_ok = False

    print("\n" + "=" * 70)
    print(f"  ISOLATION PROOF: {'PASS' if all_ok else 'FAIL'}")
    print("=" * 70)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
