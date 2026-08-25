"""Runtime contract enforcement for inter-agent message payloads."""

from __future__ import annotations

from typing import Any


def validate_trade_event(msg: dict[str, Any]) -> tuple[bool, str]:
    """Validate a TRADES channel payload.

    Returns (True, "") if valid, (False, reason) if invalid.
    """
    if not isinstance(msg, dict):
        return False, f"expected dict, got {type(msg).__name__}"

    required = ("exchange", "timestamp", "price", "size", "side")
    for field in required:
        if field not in msg:
            return False, f"missing field '{field}'"

    price = msg["price"]
    size = msg["size"]
    side = msg["side"]
    ts = msg["timestamp"]

    if not isinstance(price, (int, float)) or price <= 0:
        return False, f"price must be a positive number, got {price!r}"
    if not isinstance(size, (int, float)) or size <= 0:
        return False, f"size must be a positive number, got {size!r}"
    if side not in ("buy", "sell"):
        return False, f"side must be 'buy' or 'sell', got {side!r}"
    if not isinstance(ts, (int, float)) or ts <= 0:
        return False, f"timestamp must be a positive number, got {ts!r}"

    return True, ""


def validate_l2_snapshot(msg: dict[str, Any]) -> tuple[bool, str]:
    """Validate an L2 channel payload.

    Returns (True, "") if valid, (False, reason) if invalid.
    """
    if not isinstance(msg, dict):
        return False, f"expected dict, got {type(msg).__name__}"

    required = ("exchange", "timestamp", "bids", "asks")
    for field in required:
        if field not in msg:
            return False, f"missing field '{field}'"

    ts = msg["timestamp"]
    if not isinstance(ts, (int, float)) or ts <= 0:
        return False, f"timestamp must be a positive number, got {ts!r}"

    if not isinstance(msg["bids"], list):
        return False, f"bids must be a list, got {type(msg['bids']).__name__}"
    if not isinstance(msg["asks"], list):
        return False, f"asks must be a list, got {type(msg['asks']).__name__}"

    mid = msg.get("mid_price")
    if mid is not None and (not isinstance(mid, (int, float)) or mid <= 0):
        return False, f"mid_price must be a positive number, got {mid!r}"

    return True, ""


def validate_aggregated_snapshot(msg: dict[str, Any]) -> tuple[bool, str]:
    """Validate an AGGREGATED channel payload.

    Returns (True, "") if valid, (False, reason) if invalid.
    """
    if not isinstance(msg, dict):
        return False, f"expected dict, got {type(msg).__name__}"

    required = ("timestamp", "cvd")
    for field in required:
        if field not in msg:
            return False, f"missing field '{field}'"

    ts = msg["timestamp"]
    if not isinstance(ts, (int, float)) or ts <= 0:
        return False, f"timestamp must be a positive number, got {ts!r}"

    cvd = msg["cvd"]
    if not isinstance(cvd, dict):
        return False, f"cvd must be a dict, got {type(cvd).__name__}"

    return True, ""
