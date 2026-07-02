"""
Live demo — startet den echten Exchange Agent gegen einen In-Process Redis.
Zeigt 10 Updates von binance_l2 und binance_trades live in der Konsole.
"""
import asyncio, json, sys
sys.path.insert(0, ".")

from core.orderbook import OrderBook
import ccxt.pro as ccxtpro

SHOW_L2     = 5
SHOW_TRADES = 5

async def run():
    l2_queue    = asyncio.Queue()
    trade_queue = asyncio.Queue()

    # --- Subscriber: liest aus Queue und zeigt Output ---
    async def subscriber():
        l2_count = trade_count = 0
        print("="*60)
        print("LIVE OUTPUT — Exchange Agent → Redis Channels")
        print("="*60)
        while l2_count < SHOW_L2 or trade_count < SHOW_TRADES:
            # warte auf nächste Nachricht aus beiden Queues
            done, _ = await asyncio.wait(
                [asyncio.ensure_future(l2_queue.get()),
                 asyncio.ensure_future(trade_queue.get())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                ch, data = task.result()
                if ch == "binance_l2" and l2_count < SHOW_L2:
                    l2_count += 1
                    print(f"\n[binance_l2 #{l2_count}]")
                    print(f"  mid_price:    {data['mid_price']:.2f}")
                    print(f"  spread:       {data['spread']:.4f}")
                    print(f"  imbalance_5:  {data['imbalance_5']:.3f}")
                    print(f"  imbalance_20: {data['imbalance_20']:.3f}")
                    print(f"  bids levels:  {len(data['bids'])}  asks levels: {len(data['asks'])}")
                    print(f"  best bid:     {data['bids'][0]}")
                    print(f"  best ask:     {data['asks'][0]}")
                    print(f"  update_id:    {data['last_update_id']}")
                elif ch == "binance_trades" and trade_count < SHOW_TRADES:
                    trade_count += 1
                    print(f"\n[binance_trades #{trade_count}]")
                    print(f"  price:         {data['price']}")
                    print(f"  size:          {data['size']}")
                    print(f"  aggressor_side:{data['aggressor_side']}")
                    print(f"  trade_id:      {data['trade_id']}")
        print("\n" + "="*60)
        print("Demo complete — Agent is working correctly!")
        print("="*60)

    # --- Agent: publiziert in Queue statt Redis ---
    async def agent():
        exchange = ccxtpro.binance()
        ob = OrderBook("BTC/USDT", depth=100)
        initialized = False

        async def book_loop():
            nonlocal initialized
            while True:
                data = await exchange.watch_order_book("BTC/USDT", limit=100)
                update_id = data.get("nonce") or 0
                if not initialized:
                    ob.apply_snapshot(data["bids"], data["asks"], last_update_id=update_id)
                    initialized = True
                else:
                    ob.apply_delta(data["bids"], data["asks"], update_id=update_id)
                snap    = ob.top(ob.depth)
                metrics = ob.metrics()
                payload = {
                    "exchange":       "binance",
                    "symbol":         "BTCUSDT",
                    "timestamp":      int(asyncio.get_event_loop().time() * 1000),
                    "bids":           snap["bids"],
                    "asks":           snap["asks"],
                    "imbalance_5":    metrics["imbalance_5"],
                    "imbalance_20":   metrics["imbalance_20"],
                    "spread":         metrics["spread"],
                    "mid_price":      metrics["mid_price"],
                    "last_update_id": snap["last_update_id"],
                }
                await l2_queue.put(("binance_l2", payload))

        async def trade_loop():
            while True:
                trades = await exchange.watch_trades("BTC/USDT")
                for t in trades:
                    is_buyer_maker = t.get("info", {}).get("m")
                    payload = {
                        "exchange":       "binance",
                        "symbol":         "BTCUSDT",
                        "timestamp":      int(asyncio.get_event_loop().time() * 1000),
                        "price":          float(t["price"]),
                        "size":           float(t["amount"]),
                        "aggressor_side": "sell" if is_buyer_maker else "buy",
                        "trade_id":       str(t.get("id", "")),
                    }
                    await trade_queue.put(("binance_trades", payload))

        try:
            await asyncio.gather(book_loop(), trade_loop())
        finally:
            await exchange.close()

    print("Verbinde mit Binance WebSocket...")
    agent_task = asyncio.create_task(agent())
    try:
        await asyncio.wait_for(subscriber(), timeout=30)
    finally:
        agent_task.cancel()
        try:
            await agent_task
        except (asyncio.CancelledError, Exception):
            pass

asyncio.run(run())
