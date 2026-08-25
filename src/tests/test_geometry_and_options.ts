import { first_passage_gbm, evaluate_geometry, evaluate_scale_out_geometry, realised_vol_annualised } from "../core/geometry.js";
import { PROP_FIRM_PRESETS, size_position, simulate_challenge } from "../core/propRules.js";
import { OptionsAgent } from "../core/optionsAgent.js";
import { Broker } from "../core/broker.js";

let PASS = 0;
let FAIL = 0;

function check(name: string, cond: boolean, detail = ""): void {
  if (cond) {
    PASS++;
    console.log(`  [PASS] ${name}`);
  } else {
    FAIL++;
    console.log(`  [FAIL] ${name} ${detail ? `(${detail})` : ""}`);
  }
}

function close(a: number, b: number, tol = 1e-5): boolean {
  return Math.abs(a - b) <= tol;
}

console.log("\n=== 1. First-Passage GBM Identities ===");
// Zero drift gives linear gambler's ruin split
const pZero = first_passage_gbm(100.0, 90.0, 110.0, 0.5, 0.0);
check("zero drift gives exact linear (gambler's ruin) split", close(pZero, (100 - 90) / (110 - 90), 1e-7), `p=${pZero}`);

// Arithmetic midpoint gives 0.5
const pMid = first_passage_gbm(100.0, 90.0, 110.0, 0.5, 0.0);
check("arithmetic midpoint gives exactly 0.5", close(pMid, 0.5, 1e-7));

// Drift monotonicity
const pUp = first_passage_gbm(100.0, 90.0, 110.0, 0.5, +0.4);
const pFlat = first_passage_gbm(100.0, 90.0, 110.0, 0.5, 0.0);
const pDown = first_passage_gbm(100.0, 90.0, 110.0, 0.5, -0.4);
check("drift is monotone in win probability", pDown < pFlat && pFlat < pUp, `${pDown} < ${pFlat} < ${pUp}`);

// Vol invariance under zero drift
const pVol1 = first_passage_gbm(100.0, 90.0, 110.0, 0.2, 0.0);
const pVol2 = first_passage_gbm(100.0, 90.0, 110.0, 0.9, 0.0);
check("driftless result is vol-invariant", close(pVol1, pVol2, 1e-7));

console.log("\n=== 2. Evaluate Geometry & Breakeven ===");
const geo = evaluate_geometry(105000, 104500, 106500, 0.55, 0.0, 105000, 0.0);
check("RRR computed correctly", close(geo.rrr, (106500 - 105000) / (105000 - 104500), 1e-4));
check("breakeven win rate is exactly 1 / (1 + RRR) when cost = 0", close(geo.breakeven_win_rate, 1 / (1 + geo.rrr), 1e-4));
check("martingale (driftless) expectancy is zero (p*rrr - (1-p) == 0)", close(geo.expectancy_r, 0.0, 1e-4), `E=${geo.expectancy_r}`);
check("sign flip distance is null under zero drift martingale", geo.sign_flip_distance_pct === null);

const geoDrift = evaluate_geometry(105000, 104500, 106500, 0.55, 0.40, 105000, 0.0);
check("positive drift yields directional edge", geoDrift.expectancy_r > geo.expectancy_r);

console.log("\n=== 2b. Multi-Barrier Scale-Out Geometry ===");
// Entry: 100, Stop: 90, T1: 110, T2: 120 (50/50 split)
const scaleOut = evaluate_scale_out_geometry(100, 90, 110, 120, 0.5, 0.0, 0.5, 0.0);
check("scale-out RRR1 and RRR2 correct", scaleOut.rrr_1 === 1.0 && scaleOut.rrr_2 === 2.0);
check("scale-out P(T1) = 0.5 under zero drift", close(scaleOut.p_t1, 0.5, 1e-4));
// P(T2 | T1) with Entry at 100 and T2 at 120 from T1=110 -> (110 - 100) / (120 - 100) = 0.5
check("conditional P(T2|T1) = 0.5 under zero drift", close(scaleOut.p_t2_given_t1, 0.5, 1e-4));
check("p_full_win + p_t1_only + p_full_loss == 1.0", close(scaleOut.p_full_win + scaleOut.p_t1_only + scaleOut.p_full_loss, 1.0, 1e-4));

console.log("\n=== 3. Prop Firm Sizing & Leverage Caps ===");
const rules = PROP_FIRM_PRESETS.breakout_10k;
const sized = size_position(rules, 100000, 300, 0.005); // stop distance 300 USD
check("risk in USD is 0.5% of 10,000 = 50 USD", close(sized.risk_usd, 50, 1e-2));
check("position size = risk / stop distance = 50 / 300 = 0.1667 BTC", close(sized.btc_amount, 0.1667, 1e-3));
check("leverage capping respected under tight stops", (() => {
  const tightSized = size_position(rules, 100000, 20, 0.005); // very tight stop
  return tightSized.effective_leverage <= rules.max_leverage && tightSized.is_leverage_capped;
})());

console.log("\n=== 4. Challenge Monte Carlo Simulation ===");
const sim = simulate_challenge(rules, 0.40, 2.0, 50, 2, 3, 1000, 60);
check("simulation executes and returns valid pass probability", sim.pass_pct >= 0 && sim.pass_pct <= 100);
check("simulation bust categories partition correctly", sim.daily_bust_count + sim.total_bust_count + sim.pass_count + sim.open_count === 1000);

const simScaleOut = simulate_challenge({
  rules,
  p_win: 0.40,
  rrr: 2.0,
  risk_usd: 50,
  cost_usd: 2,
  runs: 1000,
  scale_out: {
    weight_1: 0.5,
    weight_2: 0.5,
    rrr_1: 1.0,
    rrr_2: 2.0,
    p_t1: 0.55,
    p_t2_given_t1: 0.50,
  },
  gex_regime: "AMPLIFYING",
});
check("scale-out simulation executes and logs regime", simScaleOut.regime_conditioned?.gex_regime === "AMPLIFYING");

console.log("\n=== 5. Options Agent & GEX Modeling ===");
const broker = new Broker();
const optAgent = new OptionsAgent(broker);
optAgent.setSpotPrice(64000);
const snap = optAgent.getSnapshot();
check("options snapshot generated with valid spot", snap.spot_price === 64000);
check("zero gamma calculated or bracketed", snap.zero_gamma !== null && snap.zero_gamma > 0);
check("GEX regime identified (AMPLIFYING or DAMPENING)", snap.gex_regime === "AMPLIFYING" || snap.gex_regime === "DAMPENING");
check("top clusters populated around spot", snap.top_clusters.length > 0);
check("expected move 2-sigma generated", snap.expected_move_0dte_2sigma > snap.expected_move_0dte);
check("25-delta skew generated", typeof snap.skew_25d === "number");

console.log(`\n================================`);
console.log(`  ${PASS} tests passed, ${FAIL} tests failed`);
console.log(`================================\n`);

if (FAIL > 0) process.exit(1);
