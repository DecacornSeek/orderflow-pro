"""Minimal in-process metrics counters — no external dependencies."""

from __future__ import annotations

from typing import Dict

_counters: Dict[str, int] = {}


def increment(name: str) -> None:
    _counters[name] = _counters.get(name, 0) + 1


def get(name: str) -> int:
    return _counters.get(name, 0)


def snapshot() -> Dict[str, int]:
    return dict(_counters)


def reset_all() -> None:
    """Reset all counters — intended for tests only."""
    _counters.clear()


# Named counters used across the system
VALIDATION_TRADE_FAILED = "validation_trade_event_failed_total"
VALIDATION_L2_FAILED = "validation_l2_snapshot_failed_total"
VALIDATION_AGGREGATED_FAILED = "validation_aggregated_snapshot_failed_total"
MESSAGES_SKIPPED = "messages_skipped_total"
CONTEXT_SESSION_FAIL = "context_session_fail_total"
CONTEXT_WEEKLY_FAIL = "context_weekly_fail_total"
CONTEXT_FALLBACK = "context_fallback_total"
