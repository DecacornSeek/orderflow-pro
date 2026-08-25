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
