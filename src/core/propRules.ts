export interface PropFirmRules {
  key: string;
  label: string;
  account: number;
  target: number;
  daily_loss_limit: number;
  max_drawdown: number;
  drawdown_type: "static" | "balance";
  max_leverage: number;
  commission_rate: number; // e.g. 0.0004 for 0.04% RT
  min_trading_days: number;
  consistency_rule: number; // e.g. 0.60
  profit_split: number; // e.g. 0.80
  challenge_fee: number;
}

export const PROP_FIRM_PRESETS: Record<string, PropFirmRules> = {
  breakout_10k: {
    key: "breakout_10k",
    label: "Breakout 10K",
    account: 10000,
    target: 1000,
    daily_loss_limit: 300,
    max_drawdown: 600,
    drawdown_type: "static",
    max_leverage: 5,
    commission_rate: 0.0004,
    min_trading_days: 0,
    consistency_rule: 0,
    profit_split: 0.8,
    challenge_fee: 100,
  },
  breakout_25k: {
    key: "breakout_25k",
    label: "Breakout 25K",
    account: 25000,
    target: 2500,
    daily_loss_limit: 750,
    max_drawdown: 1500,
    drawdown_type: "static",
    max_leverage: 5,
    commission_rate: 0.0004,
    min_trading_days: 0,
    consistency_rule: 0,
    profit_split: 0.8,
    challenge_fee: 200,
  },
  fundingpips_10k: {
    key: "fundingpips_10k",
    label: "FundingPips 10K",
    account: 10000,
    target: 1000,
    daily_loss_limit: 400,
    max_drawdown: 600,
    drawdown_type: "balance",
    max_leverage: 50,
    commission_rate: 0.0,
    min_trading_days: 3,
    consistency_rule: 0.6,
    profit_split: 0.8,
    challenge_fee: 100,
  },
  fundingpips_25k: {
    key: "fundingpips_25k",
    label: "FundingPips 25K",
    account: 25000,
    target: 2500,
    daily_loss_limit: 1000,
    max_drawdown: 1500,
    drawdown_type: "balance",
    max_leverage: 50,
    commission_rate: 0.0,
    min_trading_days: 3,
    consistency_rule: 0.6,
    profit_split: 0.8,
    challenge_fee: 200,
  },
};

export interface PositionSize {
  risk_usd: number;
  risk_pct: number;
  btc_amount: number;
  notional_usd: number;
  effective_leverage: number;
  is_leverage_capped: boolean;
  max_loss_if_stopped: number;
  commission_rt: number;
  total_loss_per_stop: number;
  loss_trades_to_daily_limit: number;
  loss_trades_to_max_dd: number;
}

export function size_position(
  rules: PropFirmRules,
  spot: number,
  stop_distance_usd: number,
  risk_pct = 0.005, // default 0.5%
  slippage_pct = 0.0002 // 2 bps per side
): PositionSize {
  let risk_usd = rules.account * risk_pct;
  const stop_dist_abs = Math.max(0.01, Math.abs(stop_distance_usd));

  // Base BTC size from pure stop distance
  let btc_amount = risk_usd / stop_dist_abs;
  let notional_usd = btc_amount * spot;
  let effective_leverage = notional_usd / rules.account;
  let is_capped = false;

  // Enforce prop firm max leverage constraint
  if (effective_leverage > rules.max_leverage) {
    notional_usd = rules.account * rules.max_leverage;
    btc_amount = notional_usd / spot;
    effective_leverage = rules.max_leverage;
    risk_usd = btc_amount * stop_dist_abs;
    is_capped = true;
  }

  const commission_rt = notional_usd * (rules.commission_rate * 2 + slippage_pct * 2);
  const total_loss_per_stop = risk_usd + commission_rt;

  const loss_trades_to_daily_limit = Math.floor(rules.daily_loss_limit / total_loss_per_stop);
  const loss_trades_to_max_dd = Math.floor(rules.max_drawdown / total_loss_per_stop);

  return {
    risk_usd: Number(risk_usd.toFixed(2)),
    risk_pct: Number(((risk_usd / rules.account) * 100).toFixed(2)),
    btc_amount: Number(btc_amount.toFixed(4)),
    notional_usd: Number(notional_usd.toFixed(2)),
    effective_leverage: Number(effective_leverage.toFixed(2)),
    is_leverage_capped: is_capped,
    max_loss_if_stopped: Number(risk_usd.toFixed(2)),
    commission_rt: Number(commission_rt.toFixed(2)),
    total_loss_per_stop: Number(total_loss_per_stop.toFixed(2)),
    loss_trades_to_daily_limit: Math.max(1, loss_trades_to_daily_limit),
    loss_trades_to_max_dd: Math.max(1, loss_trades_to_max_dd),
  };
}

export interface SimulationResult {
  runs: number;
  pass_count: number;
  pass_pct: number;
  daily_bust_count: number;
  daily_bust_pct: number;
  total_bust_count: number;
  total_bust_pct: number;
  consistency_fail_count: number;
  open_count: number;
  open_pct: number;
  median_days_to_pass: number | null;
  expected_challenge_value_usd: number;
  binding_constraint: "daily_limit" | "max_drawdown" | "no_edge";
  regime_conditioned?: {
    gex_regime: "AMPLIFYING" | "DAMPENING" | "NEUTRAL";
    vol_multiplier: number;
    drift_applied: number;
  };
}

export interface ScaleOutTradeConfig {
  weight_1: number; // e.g. 0.50 (50% on T1)
  weight_2: number; // e.g. 0.50 (50% on T2 with BE stop)
  rrr_1: number;
  rrr_2: number;
  p_t1: number;
  p_t2_given_t1: number;
}

export interface SimulationOptions {
  rules: PropFirmRules;
  p_win: number;
  rrr: number;
  risk_usd: number;
  cost_usd: number;
  trades_per_day?: number;
  runs?: number;
  max_days?: number;
  scale_out?: ScaleOutTradeConfig;
  gex_regime?: "AMPLIFYING" | "DAMPENING" | "NEUTRAL";
  skew_25d?: number; // e.g. +0.03 or -0.04
}

export function simulate_challenge(
  rules_or_options: PropFirmRules | SimulationOptions,
  p_win_param?: number,
  rrr_param?: number,
  risk_usd_param?: number,
  cost_usd_param?: number,
  trades_per_day_param = 3,
  runs_param = 4000,
  max_days_param = 60
): SimulationResult {
  let rules: PropFirmRules;
  let p_win: number;
  let rrr: number;
  let risk_usd: number;
  let cost_usd: number;
  let trades_per_day = 3;
  let runs = 4000;
  let max_days = 60;
  let scale_out: ScaleOutTradeConfig | undefined;
  let gex_regime: "AMPLIFYING" | "DAMPENING" | "NEUTRAL" = "NEUTRAL";
  let skew_25d = 0.0;

  if ("key" in rules_or_options && typeof (rules_or_options as PropFirmRules).account === "number") {
    rules = rules_or_options as PropFirmRules;
    p_win = p_win_param ?? 0.40;
    rrr = rrr_param ?? 2.0;
    risk_usd = risk_usd_param ?? 50;
    cost_usd = cost_usd_param ?? 2;
    trades_per_day = trades_per_day_param;
    runs = runs_param;
    max_days = max_days_param;
  } else {
    const opts = rules_or_options as SimulationOptions;
    rules = opts.rules;
    p_win = opts.p_win;
    rrr = opts.rrr;
    risk_usd = opts.risk_usd;
    cost_usd = opts.cost_usd;
    trades_per_day = opts.trades_per_day ?? 3;
    runs = opts.runs ?? 4000;
    max_days = opts.max_days ?? 60;
    scale_out = opts.scale_out;
    gex_regime = opts.gex_regime ?? "NEUTRAL";
    skew_25d = opts.skew_25d ?? 0.0;
  }

  let vol_multiplier = 1.0;
  if (gex_regime === "AMPLIFYING") {
    vol_multiplier = 1.30;
  } else if (gex_regime === "DAMPENING") {
    vol_multiplier = 0.85;
  }

  const drift_bias = skew_25d * 0.15;
  const adjusted_p_win = Math.min(0.95, Math.max(0.05, p_win + drift_bias));

  let pass_count = 0;
  let daily_bust_count = 0;
  let total_bust_count = 0;
  let consistency_fail_count = 0;
  let open_count = 0;
  const days_to_pass: number[] = [];

  let seed = 42;
  const rnd = () => {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    return seed / 0x7fffffff;
  };

  for (let r = 0; r < runs; r++) {
    let eq = rules.account;
    let peak = rules.account;
    let day = 0;
    let done = false;
    let best_day_pnl = 0;

    while (day < max_days && !done) {
      const day_start = eq;
      let day_pnl = 0;

      for (let t = 0; t < trades_per_day; t++) {
        let pnl = 0;

        if (scale_out) {
          const hit_t1 = rnd() < scale_out.p_t1;
          if (!hit_t1) {
            pnl = -risk_usd - cost_usd;
          } else {
            const hit_t2 = rnd() < scale_out.p_t2_given_t1;
            if (hit_t2) {
              const gain_t1 = scale_out.weight_1 * scale_out.rrr_1 * risk_usd;
              const gain_t2 = scale_out.weight_2 * scale_out.rrr_2 * risk_usd;
              pnl = gain_t1 + gain_t2 - cost_usd;
            } else {
              const gain_t1 = scale_out.weight_1 * scale_out.rrr_1 * risk_usd;
              pnl = gain_t1 - cost_usd;
            }
          }
        } else {
          const is_win = rnd() < adjusted_p_win;
          pnl = (is_win ? rrr * risk_usd : -risk_usd) - cost_usd;
        }

        if (pnl < 0 && gex_regime === "AMPLIFYING" && rnd() < 0.15) {
          pnl *= 1.25;
        }

        eq += pnl;
        day_pnl = eq - day_start;

        if (rules.drawdown_type === "balance") {
          peak = Math.max(peak, eq);
        }
        const floor =
          rules.drawdown_type === "static"
            ? rules.account - rules.max_drawdown
            : peak - rules.max_drawdown;

        if (day_pnl <= -rules.daily_loss_limit) {
          daily_bust_count++;
          done = true;
          break;
        }
        if (eq <= floor) {
          total_bust_count++;
          done = true;
          break;
        }
        if (eq - rules.account >= rules.target && day + 1 >= rules.min_trading_days) {
          const total_profit = eq - rules.account;
          best_day_pnl = Math.max(best_day_pnl, day_pnl);
          if (rules.consistency_rule > 0 && best_day_pnl > rules.consistency_rule * total_profit) {
            consistency_fail_count++;
            done = true;
            break;
          }
          pass_count++;
          days_to_pass.push(day + 1);
          done = true;
          break;
        }
      }

      if (!done) {
        best_day_pnl = Math.max(best_day_pnl, day_pnl);
        day++;
      }
    }

    if (!done) {
      open_count++;
    }
  }

  days_to_pass.sort((a, b) => a - b);
  const pass_pct = (pass_count / runs) * 100;
  const expected_challenge_value_usd =
    (pass_pct / 100) * (rules.target * rules.profit_split) - rules.challenge_fee;

  let binding: "daily_limit" | "max_drawdown" | "no_edge" = "max_drawdown";
  if (p_win * rrr - (1 - p_win) <= 0 && (!scale_out || scale_out.p_t1 * scale_out.rrr_1 - (1 - scale_out.p_t1) <= 0)) {
    binding = "no_edge";
  } else if (daily_bust_count > total_bust_count * 1.3) {
    binding = "daily_limit";
  }

  return {
    runs,
    pass_count,
    pass_pct: Number(pass_pct.toFixed(1)),
    daily_bust_count,
    daily_bust_pct: Number(((daily_bust_count / runs) * 100).toFixed(1)),
    total_bust_count,
    total_bust_pct: Number(((total_bust_count / runs) * 100).toFixed(1)),
    consistency_fail_count,
    open_count,
    open_pct: Number(((open_count / runs) * 100).toFixed(1)),
    median_days_to_pass: days_to_pass.length ? days_to_pass[Math.floor(days_to_pass.length / 2)] : null,
    expected_challenge_value_usd: Number(expected_challenge_value_usd.toFixed(0)),
    binding_constraint: binding,
    regime_conditioned: {
      gex_regime,
      vol_multiplier,
      drift_applied: Number(drift_bias.toFixed(4)),
    },
  };
}
