import express from "express";
import http from "http";
import path from "path";
import { WebSocketServer, WebSocket } from "ws";
import { Broker, AGGREGATED, TRADES, SIGNALS, OPTIONS } from "./src/core/broker.js";
import { CVD } from "./src/core/cvd.js";
import { OrderBook } from "./src/core/orderbook.js";
import { History } from "./src/core/history.js";
import { MarketStructureEngine } from "./src/core/marketStructure.js";
import { SignalEngine } from "./src/core/signalEngine.js";
import { ExchangeFeed } from "./src/core/exchangeFeed.js";
import { OptionsAgent } from "./src/core/optionsAgent.js";
import { evaluate_geometry, evaluate_scale_out_geometry, realised_vol_annualised } from "./src/core/geometry.js";
import { PROP_FIRM_PRESETS, size_position, simulate_challenge } from "./src/core/propRules.js";

const PORT = 3000;
const HOST = "0.0.0.0";

const app = express();
app.use(express.json());

const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: "/ws" });

// Core Engine Singletons
const broker = new Broker();
const cvd = new CVD(200);
const orderbook = new OrderBook("BTCUSDT", 100);
const history = new History(3600);
const marketStructure = new MarketStructureEngine();
const signalEngine = new SignalEngine(broker);
const exchangeFeed = new ExchangeFeed(broker, orderbook, cvd, history, marketStructure);
const optionsAgent = new OptionsAgent(broker);

// Connected WebSockets
const clients = new Set<WebSocket>();

wss.on("connection", (ws) => {
  clients.add(ws);

  // Send initial VAP volume profile state
  try {
    ws.send(
      JSON.stringify({
        type: "history",
        vap: history.getVap(),
      })
    );
  } catch (err) {
    console.error("Error sending initial history to ws client:", err);
  }

  ws.on("close", () => {
    clients.delete(ws);
  });

  ws.on("error", () => {
    clients.delete(ws);
  });
});

function broadcast(data: any): void {
  const payload = JSON.stringify(data);
  for (const client of clients) {
    if (client.readyState === WebSocket.OPEN) {
      try {
        client.send(payload);
      } catch {
        clients.delete(client);
      }
    }
  }
}

// Broker broadcast subscriptions
broker.subscribe(AGGREGATED, (msg) => {
  if (msg.mid_price) {
    optionsAgent.setSpotPrice(msg.mid_price);
  }
  broadcast({ type: "tick", ...msg });
});

broker.subscribe(TRADES, (msg) => {
  if (msg.price) {
    optionsAgent.setSpotPrice(msg.price);
  }
  broadcast({ type: "trade", ...msg });
});

broker.subscribe(SIGNALS, (msg) => {
  broadcast({ type: "signal", ...msg });
});

broker.subscribe(OPTIONS, (msg) => {
  broadcast({ type: "options", ...msg });
});

// REST API Endpoints
app.get("/history", (req, res) => {
  res.json({
    klines: history.getKlines(),
    vap: history.getVap(),
  });
});

app.get("/metrics", (req, res) => {
  res.json({
    status: "ok",
    clients_connected: clients.size,
    total_trades_processed: cvd.tradeCount,
    uptime_seconds: Math.floor(process.uptime()),
    current_cvd: cvd.snapshot(),
  });
});

app.get("/depth-history", (req, res) => {
  const lastN = req.query.last_n ? parseInt(req.query.last_n as string, 10) : 60;
  res.json(history.getDepthFrames(isNaN(lastN) ? 60 : lastN));
});

// ─────────────────────────────────────────────────────────────────────────────
// Risikoblatt BTC Endpoints
// ─────────────────────────────────────────────────────────────────────────────

app.get("/risk/state", (req, res) => {
  const obMetrics = orderbook.metrics();
  const topBook = orderbook.top(30);
  const spot = obMetrics.mid_price || cvd.lastPrice || 64500.0;
  const optSnap = optionsAgent.getSnapshot();
  const cvdSnap = cvd.snapshot();
  const structSnap = marketStructure.getSnapshot(spot, cvdSnap);

  // Compute 24h Realised Volatility from history klines if available
  const klines = history.getKlines();
  let rv = 0.52;
  if (klines.length >= 10) {
    const returns: number[] = [];
    for (let i = 1; i < klines.length; i++) {
      returns.push(Math.log(klines[i].close / klines[i - 1].close));
    }
    rv = realised_vol_annualised(returns, 525600);
  }

  // Liquidation Proxy for Longs and Shorts around spot
  const levTiers = [10, 25, 50, 100];
  const liqProxy = {
    long_liquidations: levTiers.map((lev) => ({
      leverage: lev,
      price: Number((spot * (1.0 - 1.0 / lev)).toFixed(0)),
      label: `Long ${lev}x Liq`,
    })),
    short_liquidations: levTiers.map((lev) => ({
      leverage: lev,
      price: Number((spot * (1.0 + 1.0 / lev)).toFixed(0)),
      label: `Short ${lev}x Liq`,
    })),
  };

  res.json({
    timestamp: Date.now(),
    spot,
    spread: obMetrics.spread || 0.5,
    orderbook: {
      bids: topBook.bids,
      asks: topBook.asks,
      imbalance_5: obMetrics.imbalance_5,
      imbalance_20: obMetrics.imbalance_20,
    },
    options: {
      ...optSnap,
      realised_vol_annualised: Number((rv * 100).toFixed(1)),
      iv_rv_ratio: Number((optSnap.atm_iv / (rv * 100)).toFixed(2)),
    },
    cvd: cvdSnap,
    market_structure: structSnap,
    volume_profile: {
      vap: history.getVap(),
      poc: structSnap.session_context.session_poc,
      vah: structSnap.session_context.session_value_area_high,
      val: structSnap.session_context.session_value_area_low,
    },
    liquidation_proxy: liqProxy,
    presets: PROP_FIRM_PRESETS,
  });
});

app.post("/risk/reset-anchor", (req, res) => {
  optionsAgent.resetSessionAnchor();
  res.json({ status: "ok", message: "Session anchor reset successfully." });
});

app.post("/risk/evaluate", (req, res) => {
  try {
    const {
      entry,
      stop,
      target,
      target_2,
      rules_key = "breakout_10k",
      risk_pct = 0.005,
      annual_drift = 0.0,
      annual_vol = 0.52,
      cost_r = 0.04,
      manual_win_rate,
      trades_per_day = 3,
      gex_regime,
      skew_25d,
    } = req.body;

    const numEntry = parseFloat(entry);
    const numStop = parseFloat(stop);
    const numTarget = parseFloat(target);
    const numTarget2 = target_2 !== undefined && target_2 !== null ? parseFloat(target_2) : null;

    if (isNaN(numEntry) || isNaN(numStop) || isNaN(numTarget)) {
      return res.status(400).json({ error: "Invalid entry, stop, or target values." });
    }

    if (!(numStop < numEntry && numEntry < numTarget)) {
      return res.status(400).json({
        error: "Barrier geometry error: Stop must be below Entry, and Target must be above Entry.",
      });
    }

    const rules = PROP_FIRM_PRESETS[rules_key] || PROP_FIRM_PRESETS.breakout_10k;
    const stopDist = Math.abs(numEntry - numStop);

    // 1. Evaluate Trade Geometry
    const geometry = evaluate_geometry(
      numEntry,
      numStop,
      numTarget,
      parseFloat(annual_vol) || 0.52,
      parseFloat(annual_drift) || 0.0,
      numEntry,
      parseFloat(cost_r) || 0.04
    );

    // Optional Multi-Barrier Scale-Out Geometry (Tranche 1 & Tranche 2 with Breakeven Trail)
    let scaleOutGeometry = null;
    if (numTarget2 !== null && !isNaN(numTarget2) && numTarget2 > numTarget) {
      scaleOutGeometry = evaluate_scale_out_geometry(
        numEntry,
        numStop,
        numTarget,
        numTarget2,
        parseFloat(annual_vol) || 0.52,
        parseFloat(annual_drift) || 0.0,
        0.50,
        parseFloat(cost_r) || 0.04
      );
    }

    // If manual win rate provided by user, compute manual expectancy
    let manualEvaluation = null;
    if (manual_win_rate !== undefined && manual_win_rate !== null && !isNaN(manual_win_rate)) {
      const p = parseFloat(manual_win_rate);
      const ev = p * geometry.rrr - (1.0 - p) - (parseFloat(cost_r) || 0.04);
      manualEvaluation = {
        manual_p_win: p,
        manual_expectancy_r: Number(ev.toFixed(4)),
        is_positive: ev > 0,
      };
    }

    // 2. Position Sizing
    const sizing = size_position(
      rules,
      numEntry,
      stopDist,
      parseFloat(risk_pct) || 0.005,
      0.0002
    );

    // 3. Monte Carlo Challenge Simulation (Object-based with Scale-Out & GEX Regime Conditioning)
    const effectivePWin = manual_win_rate ? parseFloat(manual_win_rate) : geometry.p_target;
    const optSnap = optionsAgent.getSnapshot();
    const effectiveRegime = gex_regime || optSnap.gex_regime;
    const effectiveSkew = skew_25d !== undefined ? parseFloat(skew_25d) : optSnap.skew_25d;

    const simulation = simulate_challenge({
      rules,
      p_win: effectivePWin,
      rrr: geometry.rrr,
      risk_usd: sizing.risk_usd,
      cost_usd: sizing.commission_rt,
      trades_per_day: parseInt(trades_per_day, 10) || 3,
      runs: 4000,
      max_days: 60,
      scale_out: scaleOutGeometry
        ? {
            weight_1: scaleOutGeometry.weight_1,
            weight_2: scaleOutGeometry.weight_2,
            rrr_1: scaleOutGeometry.rrr_1,
            rrr_2: scaleOutGeometry.rrr_2,
            p_t1: scaleOutGeometry.p_t1,
            p_t2_given_t1: scaleOutGeometry.p_t2_given_t1,
          }
        : undefined,
      gex_regime: effectiveRegime,
      skew_25d: effectiveSkew,
    });

    res.json({
      geometry: {
        ...geometry,
        scale_out: scaleOutGeometry,
        manual_evaluation: manualEvaluation,
      },
      sizing,
      simulation,
      rules,
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message || "Evaluation error" });
  }
});

// Static assets
const staticDir = path.join(process.cwd(), "static");
app.use(express.static(staticDir));

app.get("/risk", (req, res) => {
  res.sendFile(path.join(staticDir, "risk.html"));
});

app.get("/risk.html", (req, res) => {
  res.sendFile(path.join(staticDir, "risk.html"));
});

app.get("*", (req, res) => {
  res.sendFile(path.join(staticDir, "index.html"));
});

// Start Engine
signalEngine.start();
optionsAgent.start();
exchangeFeed.start();

server.listen(PORT, HOST, () => {
  console.log(`[OrderFlow Pro & Risikoblatt BTC] Server listening on http://${HOST}:${PORT}`);
});
