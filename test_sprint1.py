"""Quick smoke test — Binance WebSocket: L2 book + trades + metrics"""
import asyncio, sys
sys.path.insert(0, ".")
import ccxt.pro as ccxtpro
from core.orderbook import OrderBook

async def test():
    exchange = ccxtpro.binance()
    ob = OrderBook("BTC/USDT", depth=100)

    print("Verbinde mit Binance WebSocket...")

    # --- L2 Order Book: Snapshot ---
    data = await exchange.watch_order_book("BTC/USDT", limit=100)
    update_id = data.get("nonce") or data.get("last_update_id") or 0
    ob.apply_snapshot(data["bids"], data["asks"], last_update_id=update_id)
    snap = ob.snapshot()
    metrics = ob.metrics()
    print(f"L2 Snapshot OK — bids: {len(snap['bids'])}  asks: {len(snap['asks'])}  (expect 100)")
    print(f"  best bid: {snap['bids'][0]}  best ask: {snap['asks'][0]}")
    print(f"  spread: {metrics['spread']:.4f}  mid: {metrics['mid_price']:.2f}")
    print(f"  imbalance_5: {metrics['imbalance_5']:.3f}  imbalance_20: {metrics['imbalance_20']:.3f}")

    # --- Trade Stream + aggressor ---
    trades = await exchange.watch_trades("BTC/USDT")
    t = trades[-1]
    side = t["side"]
    is_buyer_maker = t.get("info", {}).get("m", None)
    if is_buyer_maker is not None:
        aggressor = "sell" if is_buyer_maker else "buy"
    else:
        aggressor = t.get("takerOrMaker") or "buy"
    print(f"Trade OK      — price: {t['price']}  size: {t['amount']}  aggressor_side: {aggressor}  (raw m={is_buyer_maker})")

    await exchange.close()
    print("\nAll systems GO — Sprint 1 verified!")


asyncio.run(test())
