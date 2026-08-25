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
}

export function simulate_challenge(
  rules: PropFirmRules,
  p_win: number,
  rrr: number,
  risk_usd: number,
  cost_usd: number,
  trades_per_day = 3,
  runs = 4000,
  max_days = 60
): SimulationResult {
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
        const is_win = rnd() < p_win;
        const pnl = (is_win ? rrr * risk_usd : -risk_usd) - cost_usd;
        eq += pnl;
        day_pnl = eq - day_start;

        if (rules.drawdown_type === "balance") {
          peak = Math.max(peak, eq);
        }
        const floor = rules.drawdown_type === "static" ? rules.account - rules.max_drawdown : peak - rules.max_drawdown;

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
  if (p_win * rrr - (1 - p_win) <= 0) {
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
  };
}
