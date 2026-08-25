export interface TradeGeometry {
  entry: number;
  stop: number;
  target: number;
  spot: number;

  rrr: number; // reward:risk in R
  p_target: number; // implied win rate
  p_stop: number;
  p_timeout: number;

  breakeven_win_rate: number; // win rate at which expectancy_r == 0
  expectancy_r: number; // E[R] per trade, net of costs
  edge_pp: number; // p_target - breakeven in percentage points

  estimator: "gbm" | "bootstrap";
  annual_vol: number;
  annual_drift: number;
  is_positive: boolean;
  sign_flip_distance_pct: number | null;
}

/**
 * P(target touched before stop) for GBM with two absorbing barriers.
 * Under zero price drift (martingale), p_target = (spot - stop) / (target - stop) (linear gambler's ruin).
 */
export function first_passage_gbm(
  spot: number,
  stop: number,
  target: number,
  annual_vol: number,
  annual_drift = 0.0
): number {
  if (!(stop < spot && spot < target)) {
    throw new Error(
      `spot must lie strictly between stop and target, got stop=${stop}, spot=${spot}, target=${target}`
    );
  }
  if (annual_vol <= 0) {
    throw new Error(`annual_vol must be > 0, got ${annual_vol}`);
  }

  const x = Math.log(spot / stop);
  const span = Math.log(target / stop);
  const lam = (2.0 * (annual_drift - 0.5 * annual_vol * annual_vol)) / (annual_vol * annual_vol);

  if (Math.abs(lam) < 1e-9) {
    return x / span;
  }
  return (1.0 - Math.exp(-lam * x)) / (1.0 - Math.exp(-lam * span));
}

/**
 * Calculates annualised realised volatility from log returns.
 * For 1-minute bars in crypto 24/7: bars_per_year = 525,600.
 */
export function realised_vol_annualised(returns: number[], bars_per_year = 525600): number {
  const valid = returns.filter((r) => Number.isFinite(r));
  if (valid.length < 2) return 0.52; // standard baseline ~52%
  const mean = valid.reduce((a, b) => a + b, 0) / valid.length;
  const variance =
    valid.reduce((acc, r) => acc + (r - mean) * (r - mean), 0) / (valid.length - 1);
  return Math.sqrt(variance) * Math.sqrt(bars_per_year);
}

/**
 * Evaluates trade geometry under GBM first-passage.
 */
export function evaluate_geometry(
  entry: number,
  stop: number,
  target: number,
  annual_vol: number,
  annual_drift = 0.0,
  spot?: number,
  cost_r = 0.0
): TradeGeometry {
  const s = spot !== undefined && spot !== null ? spot : entry;
  if (!(stop < entry && entry < target)) {
    throw new Error(
      `entry must lie strictly between stop and target, got stop=${stop}, entry=${entry}, target=${target}`
    );
  }
  if (!(stop < s && s < target)) {
    throw new Error(
      `spot must lie strictly between stop and target, got stop=${stop}, spot=${s}, target=${target}`
    );
  }

  const risk = entry - stop;
  const reward = target - entry;
  const rrr = reward / risk;

  const p_target = first_passage_gbm(s, stop, target, annual_vol, annual_drift);
  const p_timeout = 0.0;
  const p_stop = 1.0 - p_target;

  const expectancy_r = p_target * rrr - p_stop * 1.0 - cost_r;
  const breakeven = (1.0 + cost_r) / (1.0 + rrr);
  const is_positive = expectancy_r > 0.0;

  // Calculate sign flip distance: Under zero-drift martingale, E[R] is uniformly negative (-cost_r) across all valid entry barriers.
  // A sign flip from barrier geometry alone only exists when annual_drift != 0.
  let sign_flip_distance_pct: number | null = null;
  if (Math.abs(annual_drift) >= 1e-5) {
    const lo = stop * 1.0005;
    const hi = target * 0.9995;
    if (lo < hi) {
      for (let i = 1; i <= 200; i++) {
        const testSpot = lo + ((hi - lo) * i) / 200.0;
        try {
          const pt = first_passage_gbm(testSpot, stop, target, annual_vol, annual_drift);
          const ev = pt * rrr - (1.0 - pt) - cost_r;
          if ((ev > 0) !== is_positive) {
            sign_flip_distance_pct = (Math.abs(testSpot - s) / s) * 100.0;
            break;
          }
        } catch {}
      }
    }
  }

  return {
    entry,
    stop,
    target,
    spot: s,
    rrr: Number(rrr.toFixed(4)),
    p_target: Number(p_target.toFixed(4)),
    p_stop: Number(p_stop.toFixed(4)),
    p_timeout: 0.0,
    breakeven_win_rate: Number(breakeven.toFixed(4)),
    expectancy_r: Number(expectancy_r.toFixed(4)),
    edge_pp: Number(((p_target - breakeven) * 100.0).toFixed(2)),
    estimator: "gbm",
    annual_vol,
    annual_drift,
    is_positive,
    sign_flip_distance_pct: sign_flip_distance_pct !== null ? Number(sign_flip_distance_pct.toFixed(2)) : null,
  };
}

export interface ScaleOutGeometry {
  entry: number;
  stop: number;
  target_1: number;
  target_2: number;
  weight_1: number; // e.g. 0.50 (50% on T1)
  weight_2: number; // e.g. 0.50 (50% on T2 with breakeven stop)
  
  rrr_1: number;
  rrr_2: number;
  blended_rrr: number;
  
  p_t1: number; // P(S_t hits T1 before SL_0)
  p_t2_given_t1: number; // P(S_t hits T2 before Entry | T1 reached)
  p_full_win: number; // p_t1 * p_t2_given_t1
  p_t1_only: number; // p_t1 * (1 - p_t2_given_t1)
  p_full_loss: number; // 1 - p_t1
  
  expected_pnl_r: number; // E[R] net of maker/taker cost savings
  breakeven_p1: number; // Required P1 win rate to break even with BE trail
  is_positive: boolean;
}

/**
 * Multi-Barrier First-Passage with Dynamic Scale-Out & Breakeven Trail.
 * Tranche 1: Target 1 (e.g. 1σ). P(T1 before SL0).
 * Tranche 2: Target 2 (e.g. 2σ). Stop trailed to Entry (breakeven).
 *   P(T2 before Entry | T1 hit) is conditional first-passage from T1 between Entry and T2.
 */
export function evaluate_scale_out_geometry(
  entry: number,
  stop: number,
  target_1: number,
  target_2: number,
  annual_vol: number,
  annual_drift = 0.0,
  weight_1 = 0.50,
  cost_r = 0.04
): ScaleOutGeometry {
  if (!(stop < entry && entry < target_1 && target_1 < target_2)) {
    throw new Error(
      `Monotonic order required: stop < entry < target_1 < target_2. Got stop=${stop}, entry=${entry}, t1=${target_1}, t2=${target_2}`
    );
  }
  const weight_2 = 1.0 - weight_1;
  const risk_unit = entry - stop;
  
  const rrr_1 = (target_1 - entry) / risk_unit;
  const rrr_2 = (target_2 - entry) / risk_unit;
  const blended_rrr = weight_1 * rrr_1 + weight_2 * rrr_2;

  // Step 1: P(Hit T1 before SL0)
  const p_t1 = first_passage_gbm(entry, stop, target_1, annual_vol, annual_drift);

  // Step 2: Conditional P(Hit T2 before Entry | Started at T1)
  // When T1 is touched, stop is trailed to Entry. Price is at T1, barriers are [Entry, Target2].
  const p_t2_given_t1 = first_passage_gbm(target_1, entry, target_2, annual_vol, annual_drift);

  const p_full_win = p_t1 * p_t2_given_t1;
  const p_t1_only = p_t1 * (1.0 - p_t2_given_t1);
  const p_full_loss = 1.0 - p_t1;

  // Outcome PnLs in R:
  // Full Win: weight_1 * rrr_1 + weight_2 * rrr_2 - cost_r
  // T1 Only (stopped at BE for tranche 2): weight_1 * rrr_1 + weight_2 * 0 - cost_r
  // Full Loss: -1.0 * (weight_1 + weight_2) - cost_r = -1.0 - cost_r
  const pnl_full_win = weight_1 * rrr_1 + weight_2 * rrr_2;
  const pnl_t1_only = weight_1 * rrr_1;
  const pnl_loss = -1.0;

  const expected_pnl_r =
    p_full_win * pnl_full_win + p_t1_only * pnl_t1_only + p_full_loss * pnl_loss - cost_r;

  // Breakeven P1: E[R] = 0
  // p_t1 * [ weight_1 * rrr_1 + weight_2 * p_t2_given_t1 * rrr_2 + 1.0 ] - 1.0 - cost_r = 0
  const win_payoff_multiplier = weight_1 * rrr_1 + weight_2 * p_t2_given_t1 * rrr_2 + 1.0;
  const breakeven_p1 = (1.0 + cost_r) / win_payoff_multiplier;

  return {
    entry,
    stop,
    target_1,
    target_2,
    weight_1,
    weight_2,
    rrr_1: Number(rrr_1.toFixed(4)),
    rrr_2: Number(rrr_2.toFixed(4)),
    blended_rrr: Number(blended_rrr.toFixed(4)),
    p_t1: Number(p_t1.toFixed(4)),
    p_t2_given_t1: Number(p_t2_given_t1.toFixed(4)),
    p_full_win: Number(p_full_win.toFixed(4)),
    p_t1_only: Number(p_t1_only.toFixed(4)),
    p_full_loss: Number(p_full_loss.toFixed(4)),
    expected_pnl_r: Number(expected_pnl_r.toFixed(4)),
    breakeven_p1: Number(breakeven_p1.toFixed(4)),
    is_positive: expected_pnl_r > 0,
  };
}
