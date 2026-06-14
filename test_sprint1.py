"""Quick smoke test — Binance WebSocket: L2 book + trades"""
import asyncio, sys
sys.path.insert(0, ".")
import ccxt.pro as ccxtpro
from core.orderbook import OrderBook

async def test():
    exchange = ccxtpro.binance()
    ob = OrderBook("BTC/USDT", 20)

    print("Connecting to Binance WebSocket...")

    # Test L2 order book
    data = await exchange.watch_order_book("BTC/USDT")
    ob.update(data["bids"], data["asks"])
    snap = ob.snapshot()
    print(f"L2 Book OK  — best bid: {snap['bids'][0]}  best ask: {snap['asks'][0]}")
    print(f"             bids depth: {len(snap['bids'])}  asks depth: {len(snap['asks'])}")

    # Test trade stream + aggressor derivation
    trades = await exchange.watch_trades("BTC/USDT")
    t = trades[-1]
    side = t["side"]
    is_buyer_maker = t.get("info", {}).get("m", None)
    if is_buyer_maker is not None:
        aggressor = "taker" if (side == "sell" and is_buyer_maker) or (side == "buy" and not is_buyer_maker) else "maker"
    else:
        aggressor = t.get("takerOrMaker") or "taker"
    print(f"Trade OK    — price: {t['price']}  size: {t['amount']}  side: {side}  aggressor: {aggressor}  (raw m={is_buyer_maker})")

    await exchange.close()
    print("\nAll systems GO — Sprint 1 WebSocket working!")

asyncio.run(test())
