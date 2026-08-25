export type PriceLevel = [number, number]; // [price, size]

export class OrderBook {
  public symbol: string;
  public depth: number;
  public bids: Map<number, number> = new Map(); // price -> size
  public asks: Map<number, number> = new Map(); // price -> size
  public lastUpdateId = 0;

  constructor(symbol = "BTCUSDT", depth = 100) {
    this.symbol = symbol;
    this.depth = depth;
  }

  private trimBids(): void {
    if (this.bids.size <= this.depth) return;
    const sortedPrices = Array.from(this.bids.keys()).sort((a, b) => b - a);
    const toRemove = sortedPrices.slice(this.depth);
    for (const p of toRemove) {
      this.bids.delete(p);
    }
  }

  private trimAsks(): void {
    if (this.asks.size <= this.depth) return;
    const sortedPrices = Array.from(this.asks.keys()).sort((a, b) => a - b);
    const toRemove = sortedPrices.slice(this.depth);
    for (const p of toRemove) {
      this.asks.delete(p);
    }
  }

  topBids(n: number): PriceLevel[] {
    const prices = Array.from(this.bids.keys()).sort((a, b) => b - a).slice(0, n);
    return prices.map((p) => [p, this.bids.get(p)!]);
  }

  topAsks(n: number): PriceLevel[] {
    const prices = Array.from(this.asks.keys()).sort((a, b) => a - b).slice(0, n);
    return prices.map((p) => [p, this.asks.get(p)!]);
  }

  top(n: number) {
    return {
      bids: this.topBids(n),
      asks: this.topAsks(n),
      last_update_id: this.lastUpdateId,
    };
  }

  applySnapshot(bidsRaw: [number | string, number | string][], asksRaw: [number | string, number | string][], lastUpdateId: number): void {
    this.bids.clear();
    for (const [p, s] of bidsRaw) {
      const price = typeof p === "number" ? p : parseFloat(p);
      const size = typeof s === "number" ? s : parseFloat(s);
      if (size > 0 && !isNaN(price)) {
        this.bids.set(price, size);
      }
    }

    this.asks.clear();
    for (const [p, s] of asksRaw) {
      const price = typeof p === "number" ? p : parseFloat(p);
      const size = typeof s === "number" ? s : parseFloat(s);
      if (size > 0 && !isNaN(price)) {
        this.asks.set(price, size);
      }
    }

    this.trimBids();
    this.trimAsks();
    this.lastUpdateId = lastUpdateId;
  }

  metrics() {
    const bestBid = this.bids.size > 0 ? Math.max(...Array.from(this.bids.keys())) : null;
    const bestAsk = this.asks.size > 0 ? Math.min(...Array.from(this.asks.keys())) : null;

    let spread: number | null = null;
    let midPrice: number | null = null;

    if (bestBid !== null && bestAsk !== null) {
      spread = Number((bestAsk - bestBid).toFixed(2));
      midPrice = Number(((bestBid + bestAsk) / 2).toFixed(2));
    }

    const calculateImbalance = (n: number): number | null => {
      const topB = this.topBids(n);
      const topA = this.topAsks(n);
      const bidVol = topB.reduce((acc, [, s]) => acc + s, 0);
      const askVol = topA.reduce((acc, [, s]) => acc + s, 0);
      const total = bidVol + askVol;
      if (total === 0) return null;
      return Number(((bidVol - askVol) / total).toFixed(4));
    };

    return {
      spread,
      mid_price: midPrice,
      best_bid: bestBid,
      best_ask: bestAsk,
      imbalance_5: calculateImbalance(5),
      imbalance_20: calculateImbalance(20),
    };
  }
}
