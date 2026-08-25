export interface CVDTrade {
  price: number;
  size: number;
  aggressorSide: "buy" | "sell";
  delta: number;
  timestamp?: number;
}

export interface CVDSnapshot {
  cumulative_buy_volume: number;
  cumulative_sell_volume: number;
  cumulative_delta: number;
  rolling_buy_volume: number;
  rolling_sell_volume: number;
  rolling_delta: number;
  cvd_ratio: number | null;
  trade_count: number;
  last_price: number | null;
  last_timestamp: number | null;
}

export class CVD {
  public windowSize: number;
  public cumulativeBuyVolume = 0;
  public cumulativeSellVolume = 0;
  public cumulativeDelta = 0;

  private window: CVDTrade[] = [];
  private _rollingDelta = 0;
  private _rollingBuy = 0;
  private _rollingSell = 0;

  public tradeCount = 0;
  public lastPrice: number | null = null;
  public lastTimestamp: number | null = null;

  constructor(windowSize = 200) {
    this.windowSize = windowSize;
  }

  update(price: number, size: number, aggressorSide: "buy" | "sell", timestamp?: number): CVDSnapshot {
    const side = aggressorSide.toLowerCase() === "buy" ? "buy" : "sell";
    const delta = side === "buy" ? size : -size;

    if (side === "buy") {
      this.cumulativeBuyVolume += size;
    } else {
      this.cumulativeSellVolume += size;
    }
    this.cumulativeDelta += delta;

    if (this.window.length >= this.windowSize) {
      const oldest = this.window.shift()!;
      this._rollingDelta -= oldest.delta;
      if (oldest.aggressorSide === "buy") {
        this._rollingBuy -= oldest.size;
      } else {
        this._rollingSell -= oldest.size;
      }
    }

    const tradeRecord: CVDTrade = {
      price,
      size,
      aggressorSide: side,
      delta,
      timestamp,
    };
    this.window.push(tradeRecord);
    this._rollingDelta += delta;
    if (side === "buy") {
      this._rollingBuy += size;
    } else {
      this._rollingSell += size;
    }

    this.tradeCount++;
    this.lastPrice = price;
    if (timestamp !== undefined) {
      this.lastTimestamp = timestamp;
    }

    return this.snapshot();
  }

  reset(): void {
    this.cumulativeBuyVolume = 0;
    this.cumulativeSellVolume = 0;
    this.cumulativeDelta = 0;
    this.window = [];
    this._rollingDelta = 0;
    this._rollingBuy = 0;
    this._rollingSell = 0;
    this.tradeCount = 0;
    this.lastPrice = null;
    this.lastTimestamp = null;
  }

  snapshot(): CVDSnapshot {
    const total = this._rollingBuy + this._rollingSell;
    const ratio = total > 0 ? Number(((this._rollingBuy - this._rollingSell) / total).toFixed(6)) : null;

    return {
      cumulative_buy_volume: Number(this.cumulativeBuyVolume.toFixed(6)),
      cumulative_sell_volume: Number(this.cumulativeSellVolume.toFixed(6)),
      cumulative_delta: Number(this.cumulativeDelta.toFixed(6)),
      rolling_buy_volume: Number(this._rollingBuy.toFixed(6)),
      rolling_sell_volume: Number(this._rollingSell.toFixed(6)),
      rolling_delta: Number(this._rollingDelta.toFixed(6)),
      cvd_ratio: ratio,
      trade_count: this.tradeCount,
      last_price: this.lastPrice,
      last_timestamp: this.lastTimestamp,
    };
  }

  get rollingDelta(): number {
    return this._rollingDelta;
  }
}
