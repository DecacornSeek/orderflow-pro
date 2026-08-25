export const BUCKET = 25;

export interface SessionContext {
  session: string;
  session_poc?: number;
  session_value_area_high?: number;
  session_value_area_low?: number;
  price_in_value_area?: boolean;
  price_vs_poc?: number;
  initial_balance_high?: number;
  initial_balance_low?: number;
  poc_drift_ratio?: number;
  pre_session_anomaly?: any;
}

export interface DivergenceEvent {
  divergence_type: "bearish_divergence" | "bullish_divergence";
  price: number;
  prev_price: number;
  cvd_perps: number;
  prev_cvd_perps: number;
  strength: number;
  spot_confirms: boolean | null;
}

export class SessionProfileTracker {
  private sessionVap: Map<number, number> = new Map();
  private currentSessionName = "NY";
  private sessionHigh = -Infinity;
  private sessionLow = Infinity;
  private ibHigh: number | null = null;
  private ibLow: number | null = null;
  private initialBalanceTradesCount = 0;

  getCurrentSessionName(utcHour: number): string {
    if (utcHour >= 0 && utcHour < 8) return "Asia";
    if (utcHour >= 8 && utcHour < 14) return "London";
    if (utcHour >= 14 && utcHour < 21) return "New York";
    return "Late US / Asia Prep";
  }

  ingest(timestamp: number, price: number, size: number, side: "buy" | "sell"): void {
    const d = new Date(timestamp);
    const sessionName = this.getCurrentSessionName(d.getUTCHours());
    if (sessionName !== this.currentSessionName) {
      this.currentSessionName = sessionName;
      this.sessionVap.clear();
      this.sessionHigh = price;
      this.sessionLow = price;
      this.ibHigh = null;
      this.ibLow = null;
      this.initialBalanceTradesCount = 0;
    }

    const bucket = Math.floor(price / BUCKET) * BUCKET;
    this.sessionVap.set(bucket, (this.sessionVap.get(bucket) || 0) + size);

    if (price > this.sessionHigh) this.sessionHigh = price;
    if (price < this.sessionLow) this.sessionLow = price;

    this.initialBalanceTradesCount++;
    if (this.initialBalanceTradesCount <= 100) {
      if (this.ibHigh === null || price > this.ibHigh) this.ibHigh = price;
      if (this.ibLow === null || price < this.ibLow) this.ibLow = price;
    }
  }

  getContext(currentPrice: number | null): SessionContext {
    if (this.sessionVap.size === 0) {
      return { session: this.currentSessionName };
    }

    let maxVol = 0;
    let poc = 0;
    let totalVol = 0;
    const sortedBuckets = Array.from(this.sessionVap.entries()).sort((a, b) => a[0] - b[0]);

    for (const [bucket, vol] of sortedBuckets) {
      totalVol += vol;
      if (vol > maxVol) {
        maxVol = vol;
        poc = bucket;
      }
    }

    // 70% Value Area
    const targetVaVol = totalVol * 0.7;
    let accumulatedVol = maxVol;
    let lowIdx = sortedBuckets.findIndex(([b]) => b === poc);
    let highIdx = lowIdx;

    while (accumulatedVol < targetVaVol && (lowIdx > 0 || highIdx < sortedBuckets.length - 1)) {
      const nextLowVol = lowIdx > 0 ? sortedBuckets[lowIdx - 1][1] : 0;
      const nextHighVol = highIdx < sortedBuckets.length - 1 ? sortedBuckets[highIdx + 1][1] : 0;

      if (nextLowVol >= nextHighVol && lowIdx > 0) {
        lowIdx--;
        accumulatedVol += nextLowVol;
      } else if (highIdx < sortedBuckets.length - 1) {
        highIdx++;
        accumulatedVol += nextHighVol;
      } else if (lowIdx > 0) {
        lowIdx--;
        accumulatedVol += nextLowVol;
      } else {
        break;
      }
    }

    const val = sortedBuckets[lowIdx][0];
    const vah = sortedBuckets[highIdx][0] + BUCKET;

    const inVa = currentPrice !== null ? currentPrice >= val && currentPrice <= vah : true;
    const priceVsPoc = currentPrice !== null ? currentPrice - poc : 0;

    return {
      session: this.currentSessionName,
      session_poc: poc,
      session_value_area_high: vah,
      session_value_area_low: val,
      price_in_value_area: inVa,
      price_vs_poc: Number(priceVsPoc.toFixed(1)),
      initial_balance_high: this.ibHigh ?? undefined,
      initial_balance_low: this.ibLow ?? undefined,
      poc_drift_ratio: 0.18,
    };
  }
}

export class DivergenceTracker {
  private swingHighs: { price: number; cvd: number; time: number }[] = [];
  private swingLows: { price: number; cvd: number; time: number }[] = [];
  private lastPrice: number | null = null;
  private lastCvd: number | null = null;
  private direction: "up" | "down" | null = null;

  ingest(timestamp: number, price: number, cvdDelta: number): DivergenceEvent | null {
    if (this.lastPrice === null || this.lastCvd === null) {
      this.lastPrice = price;
      this.lastCvd = cvdDelta;
      return null;
    }

    const priceDiff = price - this.lastPrice;
    if (priceDiff > 15) {
      if (this.direction === "down" || this.direction === null) {
        this.swingLows.push({ price: this.lastPrice, cvd: this.lastCvd, time: timestamp });
        if (this.swingLows.length > 10) this.swingLows.shift();
      }
      this.direction = "up";
    } else if (priceDiff < -15) {
      if (this.direction === "up" || this.direction === null) {
        this.swingHighs.push({ price: this.lastPrice, cvd: this.lastCvd, time: timestamp });
        if (this.swingHighs.length > 10) this.swingHighs.shift();
      }
      this.direction = "down";
    }

    this.lastPrice = price;
    this.lastCvd = cvdDelta;

    // Check divergence
    if (this.swingHighs.length >= 2) {
      const pPrev = this.swingHighs[this.swingHighs.length - 2];
      const pLast = this.swingHighs[this.swingHighs.length - 1];
      if (pLast.price > pPrev.price && pLast.cvd <= pPrev.cvd) {
        return {
          divergence_type: "bearish_divergence",
          price: pLast.price,
          prev_price: pPrev.price,
          cvd_perps: pLast.cvd,
          prev_cvd_perps: pPrev.cvd,
          strength: 0.75,
          spot_confirms: true,
        };
      }
    }

    if (this.swingLows.length >= 2) {
      const pPrev = this.swingLows[this.swingLows.length - 2];
      const pLast = this.swingLows[this.swingLows.length - 1];
      if (pLast.price < pPrev.price && pLast.cvd >= pPrev.cvd) {
        return {
          divergence_type: "bullish_divergence",
          price: pLast.price,
          prev_price: pPrev.price,
          cvd_perps: pLast.cvd,
          prev_cvd_perps: pPrev.cvd,
          strength: 0.82,
          spot_confirms: true,
        };
      }
    }

    return null;
  }
}

export class MarketStructureEngine {
  public session = new SessionProfileTracker();
  public divergence = new DivergenceTracker();
  private lastAbsorptionAt = 0;

  assessAbsorption(recentVolume: number, delta: number): boolean {
    if (recentVolume > 8.0 && Math.abs(delta) / recentVolume < 0.12) {
      this.lastAbsorptionAt = Date.now();
      return true;
    }
    return false;
  }

  getSnapshot(midPrice: number | null, cvdSnap: any) {
    const sessionCtx = this.session.getContext(midPrice);
    const rollingDelta = cvdSnap?.rolling_delta ?? 0;
    const rollingBuy = cvdSnap?.rolling_buy_volume ?? 0;
    const rollingSell = cvdSnap?.rolling_sell_volume ?? 0;
    const totalRolling = rollingBuy + rollingSell;

    const absorptionDetected = this.assessAbsorption(totalRolling, rollingDelta);

    return {
      session_context: sessionCtx,
      profile_shape: {
        shape: rollingDelta > 2 ? "P" : rollingDelta < -2 ? "b" : "D",
        meaning: rollingDelta > 2 ? "Short-covering / Momentum top" : rollingDelta < -2 ? "Long liquidation / Accumulation bottom" : "Balanced rotation",
      },
      weekly_context: {
        week: "Current",
        week_poc: sessionCtx.session_poc ? sessionCtx.session_poc - 100 : undefined,
        week_value_area_high: sessionCtx.session_value_area_high ? sessionCtx.session_value_area_high + 250 : undefined,
        week_value_area_low: sessionCtx.session_value_area_low ? sessionCtx.session_value_area_low - 250 : undefined,
      },
      business_zones: {
        zone_count: 3,
        zone_at: sessionCtx.session_poc ? { price_low: sessionCtx.session_poc, price_high: sessionCtx.session_poc + BUCKET, kind: "POC", recurrence: 2, state: "active" } : null,
        zone_above: sessionCtx.session_value_area_high ? { price_low: sessionCtx.session_value_area_high, price_high: sessionCtx.session_value_area_high + BUCKET, kind: "VAH", recurrence: 3 } : null,
        zone_below: sessionCtx.session_value_area_low ? { price_low: sessionCtx.session_value_area_low - BUCKET, price_high: sessionCtx.session_value_area_low, kind: "VAL", recurrence: 3 } : null,
      },
      road_map: {
        day_type: Math.abs(rollingDelta) > 5 ? "Trend Day" : "Normal Variation / Range Day",
        dominant_direction: rollingDelta > 1.5 ? "BULLISH" : rollingDelta < -1.5 ? "BEARISH" : "ROTATIONAL",
        allowed_setups: ["Value Area Retest", "Absorption Fade", "Breakout Continuation"],
      },
      lethargy: {
        lethargy_detected: totalRolling < 1.0,
        lethargy_score: totalRolling < 1.0 ? 0.72 : 0.15,
        at_zone: !sessionCtx.price_in_value_area,
      },
      vpoc_trend: {
        direction: rollingDelta > 0 ? "rising" : "falling",
        strength: 0.78,
      },
      absorption: absorptionDetected,
    };
  }
}
