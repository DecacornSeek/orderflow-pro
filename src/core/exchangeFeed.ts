import WebSocket from "ws";
import { Broker, L2, TRADES, AGGREGATED } from "./broker.js";
import { OrderBook } from "./orderbook.js";
import { CVD } from "./cvd.js";
import { History, Kline } from "./history.js";
import { MarketStructureEngine } from "./marketStructure.js";

export class ExchangeFeed {
  private broker: Broker;
  private orderbook: OrderBook;
  private cvd: CVD;
  private history: History;
  private marketStructure: MarketStructureEngine;

  private tradeWs: WebSocket | null = null;
  private depthWs: WebSocket | null = null;
  private isSimulating = false;
  private simInterval: NodeJS.Timeout | null = null;
  private aggInterval: NodeJS.Timeout | null = null;
  private currentPrice = 64500.0;
  private isRunning = false;

  constructor(
    broker: Broker,
    orderbook: OrderBook,
    cvd: CVD,
    history: History,
    marketStructure: MarketStructureEngine
  ) {
    this.broker = broker;
    this.orderbook = orderbook;
    this.cvd = cvd;
    this.history = history;
    this.marketStructure = marketStructure;
  }

  async start(): Promise<void> {
    this.isRunning = true;
    // 1. Fetch initial klines
    await this.fetchKlines();

    // 2. Start Aggregator Publish Loop (1 Hz)
    this.aggInterval = setInterval(() => {
      this.publishAggregatedSnapshot();
    }, 1000);

    // 3. Connect to live Binance WebSockets
    this.connectBinance();
  }

  stop(): void {
    this.isRunning = false;
    if (this.tradeWs) this.tradeWs.close();
    if (this.depthWs) this.depthWs.close();
    if (this.simInterval) clearInterval(this.simInterval);
    if (this.aggInterval) clearInterval(this.aggInterval);
  }

  private async fetchKlines(): Promise<void> {
    try {
      const endpoints = [
        "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=500",
        "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=500",
      ];
      let data: any = null;
      for (const url of endpoints) {
        try {
          const res = await fetch(url, { signal: AbortSignal.timeout(4000) });
          if (res.ok) {
            data = await res.json();
            break;
          }
        } catch {}
      }

      if (Array.isArray(data) && data.length > 0) {
        const klines: Kline[] = data.map((d: any) => ({
          time: Math.floor(d[0] / 1000),
          open: parseFloat(d[1]),
          high: parseFloat(d[2]),
          low: parseFloat(d[3]),
          close: parseFloat(d[4]),
          volume: parseFloat(d[5]),
        }));
        this.history.setKlines(klines);
        this.currentPrice = klines[klines.length - 1].close;
        console.log(`[ExchangeFeed] Loaded ${klines.length} 1m klines from Binance. Current BTC: $${this.currentPrice}`);
        return;
      }
    } catch (e) {
      console.warn("[ExchangeFeed] Could not fetch Binance REST klines, using synthetic baseline:", e);
    }

    // Generate realistic historical synthetic candles if network unavailable
    const nowSec = Math.floor(Date.now() / 1000 / 60) * 60;
    const syntheticKlines: Kline[] = [];
    let p = 64200.0;
    for (let i = 500; i >= 0; i--) {
      const t = nowSec - i * 60;
      const change = (Math.random() - 0.49) * 45;
      const open = p;
      const close = p + change;
      const high = Math.max(open, close) + Math.random() * 20;
      const low = Math.min(open, close) - Math.random() * 20;
      const volume = 2.5 + Math.random() * 15;
      syntheticKlines.push({
        time: t,
        open: Number(open.toFixed(2)),
        high: Number(high.toFixed(2)),
        low: Number(low.toFixed(2)),
        close: Number(close.toFixed(2)),
        volume: Number(volume.toFixed(4)),
      });
      p = close;
    }
    this.history.setKlines(syntheticKlines);
    this.currentPrice = p;
  }

  private connectBinance(): void {
    let wsConnected = false;
    const wsUrl = "wss://stream.binance.com:9443/ws/btcusdt@trade/btcusdt@depth20@100ms";

    try {
      const ws = new WebSocket(wsUrl);
      this.tradeWs = ws;

      const connectTimeout = setTimeout(() => {
        if (!wsConnected) {
          console.log("[ExchangeFeed] Binance WS connection timed out, engaging high-fidelity live orderflow simulator.");
          this.startSimulation();
        }
      }, 5000);

      ws.on("open", () => {
        wsConnected = true;
        clearTimeout(connectTimeout);
        if (this.simInterval) {
          clearInterval(this.simInterval);
          this.isSimulating = false;
        }
        console.log("[ExchangeFeed] Connected to live Binance Order Flow stream.");
      });

      ws.on("message", (raw: Buffer | string) => {
        try {
          const msg = JSON.parse(raw.toString());
          if (msg.e === "trade") {
            // Live Trade
            const price = parseFloat(msg.p);
            const size = parseFloat(msg.q);
            const side = msg.m ? "sell" : "buy";
            const ts = msg.T || Date.now();
            this.handleTrade(price, size, side, ts);
          } else if (msg.bids && msg.asks) {
            // Live Depth20
            this.orderbook.applySnapshot(msg.bids, msg.asks, msg.lastUpdateId || Date.now());
            const metrics = this.orderbook.metrics();
            const top = this.orderbook.top(20);
            this.broker.publish(L2, {
              exchange: "binance",
              timestamp: Date.now(),
              bids: top.bids,
              asks: top.asks,
              ...metrics,
            });
          }
        } catch (e) {
          console.error("Error processing Binance message:", e);
        }
      });

      ws.on("error", (err) => {
        console.warn("[ExchangeFeed] Binance WS error:", err.message);
        if (!this.isSimulating) this.startSimulation();
      });

      ws.on("close", () => {
        console.warn("[ExchangeFeed] Binance WS closed. Reconnecting or simulating...");
        if (!this.isSimulating) this.startSimulation();
        if (this.isRunning) {
          setTimeout(() => this.connectBinance(), 10000);
        }
      });
    } catch (err) {
      console.warn("[ExchangeFeed] WS init error:", err);
      this.startSimulation();
    }
  }

  private handleTrade(price: number, size: number, side: "buy" | "sell", timestamp: number): void {
    this.currentPrice = price;
    this.cvd.update(price, size, side, timestamp);
    this.history.addTrade(price, size);
    this.marketStructure.session.ingest(timestamp, price, size, side);

    const divEvent = this.marketStructure.divergence.ingest(timestamp, price, this.cvd.rollingDelta);
    if (divEvent) {
      // Divergence detected
    }

    this.broker.publish(TRADES, {
      exchange: "binance",
      timestamp,
      price,
      size,
      side,
    });
  }

  private publishAggregatedSnapshot(): void {
    const metrics = this.orderbook.metrics();
    const top = this.orderbook.top(20);
    const cvdSnap = this.cvd.snapshot();
    const struct = this.marketStructure.getSnapshot(metrics.mid_price || this.currentPrice, cvdSnap);

    const payload = {
      timestamp: Date.now(),
      mid_price: metrics.mid_price || this.currentPrice,
      best_bid: metrics.best_bid || this.currentPrice - 0.5,
      best_ask: metrics.best_ask || this.currentPrice + 0.5,
      spread: metrics.spread || 1.0,
      imbalance_5: metrics.imbalance_5 !== null ? metrics.imbalance_5 : 0.08,
      imbalance_20: metrics.imbalance_20 !== null ? metrics.imbalance_20 : 0.04,
      bids: top.bids,
      asks: top.asks,
      cvd: cvdSnap,
      ...struct,
    };

    this.history.addSnapshot(payload);
    this.broker.publish(AGGREGATED, payload);
  }

  private startSimulation(): void {
    if (this.isSimulating) return;
    this.isSimulating = true;
    console.log("[ExchangeFeed] High-fidelity OrderFlow simulation active.");

    // Populate initial orderbook around current price
    this.refreshSimulatedBook();

    // High frequency trade generator (every 80-250ms)
    const tick = () => {
      if (!this.isRunning || !this.isSimulating) return;
      const jitter = (Math.random() - 0.495) * 4.0;
      this.currentPrice = Number((this.currentPrice + jitter).toFixed(2));
      const size = Number((0.02 + Math.random() * 1.8).toFixed(4));
      const side: "buy" | "sell" = Math.random() > 0.48 ? "buy" : "sell";
      const ts = Date.now();

      this.handleTrade(this.currentPrice, size, side, ts);

      if (Math.random() > 0.4) {
        this.refreshSimulatedBook();
      }

      const nextDelay = 70 + Math.floor(Math.random() * 220);
      this.simInterval = setTimeout(tick, nextDelay);
    };

    this.simInterval = setTimeout(tick, 100);
  }

  private refreshSimulatedBook(): void {
    const bids: [number, number][] = [];
    const asks: [number, number][] = [];
    const base = this.currentPrice;

    for (let i = 1; i <= 25; i++) {
      const bidP = Number((base - i * 0.5).toFixed(2));
      const bidS = Number((0.1 + Math.random() * 4.5 + (i === 5 ? 8.0 : 0)).toFixed(4));
      bids.push([bidP, bidS]);

      const askP = Number((base + i * 0.5).toFixed(2));
      const askS = Number((0.1 + Math.random() * 4.5 + (i === 7 ? 6.5 : 0)).toFixed(4));
      asks.push([askP, askS]);
    }

    this.orderbook.applySnapshot(bids, asks, Date.now());
    const metrics = this.orderbook.metrics();
    const top = this.orderbook.top(20);

    this.broker.publish(L2, {
      exchange: "simulation",
      timestamp: Date.now(),
      bids: top.bids,
      asks: top.asks,
      ...metrics,
    });
  }
}
