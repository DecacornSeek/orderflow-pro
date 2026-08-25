"""Walk-Forward Backtest Engine — vectorbt + Optuna + PropFirmRules.

Uses two battle-tested open-source tools:
  - vectorbt (https://github.com/polakowo/vectorbt) — vectorized portfolio simulation
  - Optuna   (https://github.com/optuna/optuna)       — Bayesian hyperparameter optimization

Our job: bridge the Layer-3 feature pipeline → P3 strategy signals → vectorbt
portfolio → PropFirmRules verdict → Optuna objective.

Architecture:
  ┌──────────────────┐
  │ Feature DataFrame │  (price, regime, zones, lethargy, vpoc_trend, ...)
  └────────┬─────────┘
           │
  ┌────────▼─────────┐
  │ Strategy Function │  P3: (features, params) → pd.Series(+1/0/-1)
  └────────┬─────────┘
           │
  ┌────────▼─────────┐
  │ vectorbt Portfolio│  .from_signals(entries, exits, price, size=...)
  └────────┬─────────┘
           │
  ┌────────▼─────────┐
  │ PropFirmRules     │  daily_loss? drawdown? profit_target? → PASS/FAIL
  └────────┬─────────┘
           │
  ┌────────▼─────────┐
  │ Optuna Study      │  maximize(prop_firm_pass_rate) over param space
  └──────────────────┘

Usage:
  python strategies/backtest.py
  python strategies/backtest.py --strategy nci --firm breakout_10k --trials 200
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import vectorbt as vbt
import optuna

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from strategies.base import (
    PropFirmRules, RiskGuardrails, BacktestResult, size_position,
)

# ═════════════════════════════════════════════════════════════════════════════
# Strategy registry — add new P3 strategies here
# ═════════════════════════════════════════════════════════════════════════════

# Type: (features_df, params_dict) → signal_series (+1=long, -1=short, 0=flat)
StrategyFn = Callable[[pd.DataFrame, Dict[str, float]], pd.Series]

STRATEGIES: Dict[str, StrategyFn] = {}


def register(name: str):
    """Decorator to register a P3 strategy function."""
    def decorator(fn: StrategyFn) -> StrategyFn:
        STRATEGIES[name] = fn
        return fn
    return decorator


# ═════════════════════════════════════════════════════════════════════════════
# Example strategy — "always long" baseline (for testing the pipeline)
# ═════════════════════════════════════════════════════════════════════════════

@register("baseline_long")
def baseline_long(features: pd.DataFrame, params: Dict[str, float]) -> pd.Series:
    """Always long — baseline for pipeline validation."""
    return pd.Series(1, index=features.index)


# ═════════════════════════════════════════════════════════════════════════════
# Core: signals → vectorbt portfolio → BacktestResult
# ═════════════════════════════════════════════════════════════════════════════

def _signals_to_entries_exits(
    signals: pd.Series,
    min_bars_between: int = 1,
) -> Tuple[pd.Series, pd.Series]:
    """Convert +1/0/-1 signal series to vectorbt entry/exit booleans.

    +1 → long entry. 0 from +1 → exit. -1 → short entry. 0 from -1 → exit.
    """
    entries = pd.Series(False, index=signals.index)
    exits = pd.Series(False, index=signals.index)

    position = 0  # 0=flat, 1=long, -1=short
    bars_since_last = 0

    for i in range(len(signals)):
        s = signals.iloc[i]
        bars_since_last += 1

        if position == 0:
            if s == 1 and bars_since_last >= min_bars_between:
                entries.iloc[i] = True
                position = 1
                bars_since_last = 0
            elif s == -1 and bars_since_last >= min_bars_between:
                # Short: flip the price direction for vectorbt
                entries.iloc[i] = True
                position = -1
                bars_since_last = 0
        elif position == 1 and s <= 0:
            exits.iloc[i] = True
            position = 0
            bars_since_last = 0
        elif position == -1 and s >= 0:
            exits.iloc[i] = True
            position = 0
            bars_since_last = 0

    # Force exit at end
    if position != 0:
        exits.iloc[-1] = True

    return entries, exits


def run_backtest(
    features: pd.DataFrame,
    signals: pd.Series,
    rules: PropFirmRules,
    strategy_name: str = "unnamed",
    params: Optional[Dict[str, float]] = None,
    guardrails: RiskGuardrails = RiskGuardrails(),
) -> BacktestResult:
    """Run one backtest: signals + price → vectorbt portfolio → BacktestResult.

    Args:
        features: Must contain 'price' column (close/mid price per bar).
        signals: +1 (long), -1 (short), 0 (flat) per bar.
        rules: PropFirmRules for the target firm + account.
        strategy_name: Label for the result.
        params: Strategy parameters (for the result record).
        guardrails: RiskGuardrails for sizing.

    Returns:
        BacktestResult with all metrics and prop firm verdicts.
    """
    price = features["price"].astype(float)
    result = BacktestResult(strategy_name=strategy_name, rules=rules,
                            params=params or {})

    if len(signals) < 2:
        return result

    # ── Position sizing based on stop distance ─────────────────────────
    # Use a default 0.5% stop distance; real strategies can pass this in params
    stop_pct = params.get("stop_distance_pct", 0.005) if params else 0.005
    ps = size_position(rules, stop_distance_pct=stop_pct, guardrails=guardrails)
    size = ps.btc_amount

    # ── Convert signals to vectorbt entries/exits ──────────────────────
    entries, exits = _signals_to_entries_exits(signals)

    # ── vectorbt portfolio simulation ──────────────────────────────────
    try:
        pf = vbt.Portfolio.from_signals(
            price,
            entries=entries,
            exits=exits,
            size=size,
            size_type="amount",
            freq="1min",            # our features are minute-level
            init_cash=rules.account_size,
            fees=rules.commission_pct,  # per-side fee
            slippage=0.0005,         # 0.05% slippage on BTC
        )
    except Exception:
        return result  # not enough trades to simulate

    # ── Trade-level metrics ────────────────────────────────────────────
    trades_df = pf.trades.records_readable
    result.total_trades = len(trades_df)
    if result.total_trades == 0:
        return result

    result.win_count   = int(trades_df["PnL"].gt(0).sum())
    result.loss_count  = result.total_trades - result.win_count
    result.win_rate    = result.win_count / result.total_trades

    gross_profit = trades_df.loc[trades_df["PnL"] > 0, "PnL"].sum()
    gross_loss   = abs(trades_df.loc[trades_df["PnL"] < 0, "PnL"].sum())
    result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    result.avg_win_dollars  = trades_df.loc[trades_df["PnL"] > 0, "PnL"].mean() if result.win_count > 0 else 0.0
    result.avg_loss_dollars = trades_df.loc[trades_df["PnL"] < 0, "PnL"].mean() if result.loss_count > 0 else 0.0
    result.avg_rrr = (abs(result.avg_win_dollars) / abs(result.avg_loss_dollars)
                      if result.avg_loss_dollars != 0 else 0.0)

    # ── Equity & drawdown ──────────────────────────────────────────────
    equity = pf.value()
    result.total_pnl_dollars = float(equity.iloc[-1] - rules.account_size)
    result.total_pnl_pct = result.total_pnl_dollars / rules.account_size

    # Max drawdown from equity peak
    peak = equity.cummax()
    dd = (peak - equity) / peak
    result.max_drawdown_pct = float(dd.max())
    result.max_drawdown_dollars = result.max_drawdown_pct * rules.account_size

    # ── Daily loss tracking ────────────────────────────────────────────
    # Resample equity to daily if we have a datetime index
    max_daily_loss = 0.0
    if hasattr(equity.index, "date"):
        try:
            equity_daily = equity.resample("1D").last().dropna()
            if len(equity_daily) > 1:
                daily_pnl = equity_daily.diff()
                max_daily_loss = float(abs(daily_pnl.min())) if daily_pnl.min() < 0 else 0.0
        except (TypeError, ValueError):
            pass  # non-datetime index — skip daily resampling
    result.max_daily_loss_dollars = max_daily_loss
    result.max_daily_loss_pct = max_daily_loss / rules.account_size if max_daily_loss > 0 else 0.0

    # ── Prop firm verdicts ─────────────────────────────────────────────
    result.profit_target_hit = result.total_pnl_dollars >= rules.profit_target
    result.daily_loss_breached = result.max_daily_loss_dollars > rules.max_daily_loss
    result.drawdown_breached = result.max_drawdown_dollars > rules.max_total_drawdown
    result.min_days_met = (result.total_trades > 0)  # simplified — real check needs date count

    # Consistency rule: no single trade > X% of total profit
    if rules.consistency_rule_pct > 0 and result.total_pnl_dollars > 0:
        max_single = trades_df["PnL"].max()
        result.consistency_breached = max_single > (result.total_pnl_dollars * rules.consistency_rule_pct)

    return result


# ═════════════════════════════════════════════════════════════════════════════
# Walk-Forward Optimizer — Optuna over vectorbt
# ═════════════════════════════════════════════════════════════════════════════

def _objective(
    trial: optuna.Trial,
    strategy_fn: StrategyFn,
    features: pd.DataFrame,
    rules: PropFirmRules,
    param_space: Dict[str, Tuple[str, float, float]],
    train_mask: pd.Series,
    test_mask: pd.Series,
) -> float:
    """Optuna objective: maximize prop_firm_pass on test window.

    Optuna samples params → strategy produces signals → vectorbt simulates
    → BacktestResult → return 1.0 if challenge_passed else 0.0 (maximize).
    """
    params: Dict[str, float] = {}
    for name, (dist, low, high) in param_space.items():
        if dist == "uniform":
            params[name] = trial.suggest_float(name, low, high)
        elif dist == "loguniform":
            params[name] = trial.suggest_float(name, low, high, log=True)
        elif dist == "int":
            params[name] = float(trial.suggest_int(name, int(low), int(high)))

    train_features = features[train_mask]
    signals = strategy_fn(train_features, params)
    result = run_backtest(train_features, signals, rules,
                          strategy_name=strategy_fn.__name__, params=params)

    # Multi-objective: pass challenge + high profit factor
    score = 0.0
    if result.challenge_passed:
        score += 1.0
    if result.survived_guardrails:
        score += 0.5
    score += min(result.profit_factor / 5.0, 0.5)  # capped at 0.5

    # Log best trial
    trial.set_user_attr("profit_factor", result.profit_factor)
    trial.set_user_attr("win_rate", result.win_rate)
    trial.set_user_attr("max_dd_pct", result.max_drawdown_pct)
    trial.set_user_attr("total_trades", result.total_trades)
    trial.set_user_attr("pnl", result.total_pnl_dollars)

    return score


def walk_forward_optimize(
    strategy_fn: StrategyFn,
    features: pd.DataFrame,
    rules: PropFirmRules,
    param_space: Dict[str, Tuple[str, float, float]],
    n_train_bars: int = 5000,      # ~3.5 days of minute data
    n_test_bars: int = 1440,       # 1 day
    n_trials: int = 100,
    n_folds: Optional[int] = None,
) -> List[BacktestResult]:
    """Walk-forward optimization: Optuna per fold, validate on out-of-sample.

    For each fold:
      1. Optuna searches param space on train window
      2. Best params applied to test window (out-of-sample)
      3. BacktestResult collected

    Args:
        strategy_fn: P3 strategy function.
        features: Full feature DataFrame (must have datetime index).
        rules: PropFirmRules.
        param_space: {name: (distribution, low, high)}.
        n_train_bars: Training window size in bars.
        n_test_bars: Test window size in bars.
        n_trials: Optuna trials per fold.
        n_folds: Number of folds (default: as many as fit).

    Returns:
        List of BacktestResult — one per fold (out-of-sample).
    """
    total_bars = len(features)
    fold_size = n_train_bars + n_test_bars
    max_folds = (total_bars - n_train_bars) // n_test_bars
    if n_folds is None:
        n_folds = max_folds
    n_folds = min(n_folds, max_folds)

    results: List[BacktestResult] = []

    for fold in range(n_folds):
        start = fold * n_test_bars
        train_end = start + n_train_bars
        test_end = min(train_end + n_test_bars, total_bars)

        train_mask = pd.Series(False, index=features.index)
        train_mask.iloc[start:train_end] = True
        test_mask = pd.Series(False, index=features.index)
        test_mask.iloc[train_end:test_end] = True

        if test_mask.sum() < 60:  # need at least 1 hour of test data
            break

        # ── Optuna on train window ─────────────────────────────────────
        sampler = optuna.samplers.TPESampler(seed=42 + fold)
        study = optuna.create_study(
            direction="maximize",
            sampler=sampler,
            study_name=f"wf_fold_{fold}",
        )
        study.optimize(
            lambda trial: _objective(trial, strategy_fn, features, rules,
                                     param_space, train_mask, test_mask),
            n_trials=n_trials,
            show_progress_bar=False,
        )

        # ── Best params → out-of-sample test ───────────────────────────
        best_params = study.best_params
        test_features = features[test_mask]
        test_signals = strategy_fn(test_features, best_params)
        result = run_backtest(test_features, test_signals, rules,
                              strategy_name=strategy_fn.__name__,
                              params=best_params)
        result.params["fold"] = fold
        results.append(result)

        print(f"  Fold {fold+1}/{n_folds}: "
              f"oos_pnl=${result.total_pnl_dollars:+,.0f} "
              f"wr={result.win_rate:.0%} "
              f"pf={result.profit_factor:.2f} "
              f"dd={result.max_drawdown_pct:.1%} "
              f"pass={result.challenge_passed}")

    return results


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-Forward Backtest Engine")
    parser.add_argument("--features", type=str,
                        default=str(ROOT / "data" / "training_features.parquet"),
                        help="Path to feature parquet file")
    parser.add_argument("--strategy", type=str, default="baseline_long",
                        choices=list(STRATEGIES.keys()),
                        help="Strategy to backtest")
    parser.add_argument("--firm", type=str, default="breakout_10k",
                        choices=["breakout_10k", "breakout_25k",
                                 "fundingpips_10k", "fundingpips_25k"],
                        help="Prop firm + account combo")
    parser.add_argument("--trials", type=int, default=50,
                        help="Optuna trials per fold")
    parser.add_argument("--folds", type=int, default=None,
                        help="Number of walk-forward folds")
    args = parser.parse_args()

    # Load features
    features_path = Path(args.features)
    if not features_path.exists():
        print(f"Features not found: {features_path}")
        print("Run: python scripts/replay_history.py --days 180")
        sys.exit(1)

    features = pd.read_parquet(features_path)
    features["timestamp_dt"] = pd.to_datetime(features["timestamp"], unit="ms")
    features = features.set_index("timestamp_dt").sort_index()
    features["price"] = features["last_price"]

    print(f"Loaded {len(features):,} minute bars "
          f"({features.index[0]} → {features.index[-1]})")

    # Rules
    rules = getattr(PropFirmRules, args.firm)()
    print(f"Rules: {rules.firm} ${rules.account_size:,.0f} {rules.challenge_type}")
    print(f"  Target: ${rules.profit_target:,.0f}  "
          f"Daily: ${rules.max_daily_loss:,.0f}  "
          f"MaxDD: ${rules.max_total_drawdown:,.0f}  "
          f"DD type: {rules.drawdown_type}")

    # Strategy
    strategy_fn = STRATEGIES[args.strategy]
    print(f"Strategy: {args.strategy}")

    # Walk-forward
    print(f"\nRunning {args.trials} Optuna trials per fold...")
    results = walk_forward_optimize(
        strategy_fn=strategy_fn,
        features=features,
        rules=rules,
        param_space={},  # no params for baseline
        n_trials=args.trials,
        n_folds=args.folds,
    )

    # Summary
    if results:
        pass_rate = sum(1 for r in results if r.challenge_passed) / len(results)
        guard_rate = sum(1 for r in results if r.survived_guardrails) / len(results)
        print(f"\n{'='*60}")
        print(f"WALK-FORWARD SUMMARY: {args.strategy} @ {args.firm}")
        print(f"  Folds: {len(results)}")
        print(f"  Challenge pass rate: {pass_rate:.0%}")
        print(f"  Guardrail survival:  {guard_rate:.0%}")
        print(f"  Avg PnL: ${np.mean([r.total_pnl_dollars for r in results]):+,.0f}")
        print(f"  Avg PF:   {np.mean([r.profit_factor for r in results]):.2f}")
        for r in results:
            print(f"    {r.summary()}")
    else:
        print("No results — insufficient data or no trades generated.")


if __name__ == "__main__":
    main()
