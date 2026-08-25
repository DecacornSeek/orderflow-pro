import { Broker, AGGREGATED, SIGNALS, PATTERNS } from "./broker.js";

export class SignalEngine {
  private broker: Broker;
  private lastSignalTime = 0;
  private signalInterval = 12000; // 12s minimum between automatic signals
  private signalCooldown = new Map<string, number>();

  constructor(broker: Broker) {
    this.broker = broker;
  }

  start(): void {
    this.broker.subscribe(AGGREGATED, (msg) => {
      this.evaluate(msg);
    });

    this.broker.subscribe(PATTERNS, (pat) => {
      if (pat && pat.absorption) {
        this.emitSignal(
          `[ABSORPTION] Passive Limit Absorption detected at $${pat.price_bucket} (Volume: ${pat.volume.toFixed(2)} BTC)`,
          "warning"
        );
      }
    });
  }

  private emitSignal(text: string, level: "info" | "warning" | "error" = "info"): void {
    const now = Date.now();
    this.lastSignalTime = now;
    this.broker.publish(SIGNALS, {
      timestamp: now,
      text,
      level,
    });
  }

  evaluate(msg: any): void {
    const now = Date.now();
    if (now - this.lastSignalTime < this.signalInterval) return;

    const cvd = msg.cvd || {};
    const rollingDelta = cvd.rolling_delta || 0;
    const imb5 = msg.imbalance_5 || 0;
    const mid = msg.mid_price || 0;
    const session = msg.session_context || {};
    const div = msg.divergence;

    // 1. Delta Divergence
    if (div && now - (this.signalCooldown.get("div") || 0) > 30000) {
      this.signalCooldown.set("div", now);
      if (div.divergence_type === "bullish_divergence") {
        this.emitSignal(
          `[BULLISH DIVERGENCE] Price made lower low while aggressive selling dried up at $${mid.toFixed(1)}. Sellers trapped.`,
          "info"
        );
        return;
      } else if (div.divergence_type === "bearish_divergence") {
        this.emitSignal(
          `[BEARISH DIVERGENCE] Price made higher high but buying volume failed to confirm at $${mid.toFixed(1)}. Exhaustion risk.`,
          "info"
        );
        return;
      }
    }

    // 2. Strong Bullish Aggression
    if (rollingDelta > 3.0 && imb5 > 0.35 && now - (this.signalCooldown.get("bull") || 0) > 25000) {
      this.signalCooldown.set("bull", now);
      this.emitSignal(
        `[BULLISH MOMENTUM] Strong aggressive market buys (${rollingDelta > 0 ? "+" : ""}${rollingDelta.toFixed(2)} BTC CVD) with ${(imb5 * 100).toFixed(0)}% bid book skew at $${mid.toFixed(1)}.`,
        "info"
      );
      return;
    }

    // 3. Strong Bearish Aggression
    if (rollingDelta < -3.0 && imb5 < -0.35 && now - (this.signalCooldown.get("bear") || 0) > 25000) {
      this.signalCooldown.set("bear", now);
      this.emitSignal(
        `[BEARISH MOMENTUM] Dominant market sells (${rollingDelta.toFixed(2)} BTC CVD) hitting bids. Order book ask-skew at $${mid.toFixed(1)}.`,
        "info"
      );
      return;
    }

    // 4. Value Area Extension
    if (session.session_poc && now - (this.signalCooldown.get("session") || 0) > 40000) {
      this.signalCooldown.set("session", now);
      const dist = mid - session.session_poc;
      if (Math.abs(dist) > 150) {
        const dir = dist > 0 ? "Above" : "Below";
        this.emitSignal(
          `[SESSION EXTENSION] Price trading $${Math.abs(dist).toFixed(0)} ${dir} Session POC ($${session.session_poc}). Watch for mean-reversion rotation.`,
          "info"
        );
        return;
      }
    }
  }
}
