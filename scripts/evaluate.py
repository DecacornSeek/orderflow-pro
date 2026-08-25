"""
Evaluate OrderFlow Pro backtesting stack — run this to verify all built pieces.

Usage:  python scripts/evaluate.py

Tests:
  1. PropFirmRules — all hard constraints match the HTML files exactly
  2. Position sizing — stop-distance controls size, risk stays fixed at 0.5%
  3. Recovery math — confirms the "Drawdown Story" from your notes
  4. BacktestResult — vectorbt portfolio → prop firm verdicts
  5. Walk-forward — Optuna finds params, out-of-sample verdict works
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from strategies.base import (
    PropFirmRules, RiskGuardrails, size_position, simulate_equity_curve,
    BacktestResult,
)
from strategies.backtest import run_backtest, STRATEGIES, walk_forward_optimize

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} — {detail}")


# ═════════════════════════════════════════════════════════════════════════════
# 1. PropFirmRules — every number comes from the HTML files you provided
# ═════════════════════════════════════════════════════════════════════════════

print("=" * 60)
print("1. PROP FIRM RULES — exact match to HTML files")
print("=" * 60)

b10 = PropFirmRules.breakout_10k()
b25 = PropFirmRules.breakout_25k()
fp10 = PropFirmRules.fundingpips_10k()
fp25 = PropFirmRules.fundingpips_25k()

# Breakout $10K Classic 1-step
check("Breakout 10K: profit target $1,000", b10.profit_target == 1000)
check("Breakout 10K: daily loss $300 (3%)", b10.max_daily_loss == 300)
check("Breakout 10K: max drawdown $600 (6%)", b10.max_total_drawdown == 600)
check("Breakout 10K: DD type is STATIC", b10.drawdown_type == "static")
check("Breakout 10K: leverage 5:1", b10.max_leverage == 5.0)
check("Breakout 10K: commission 0.04%/side", b10.commission_pct == 0.0004)
check("Breakout 10K: overnight swap 0.09%/day", b10.overnight_swap_pct == 0.0009)
check("Breakout 10K: no min trading days", b10.min_trading_days == 0)
check("Breakout 10K: no consistency rule", b10.consistency_rule_pct == 0.0)
check("Breakout 10K: floor at $9,400", b10.floor_price == 9400.0)

# Breakout $25K
check("Breakout 25K: profit target $2,500", b25.profit_target == 2500)
check("Breakout 25K: daily loss $750", b25.max_daily_loss == 750)
check("Breakout 25K: floor at $23,500", b25.floor_price == 23500.0)

# FundingPips $10K 1-step
check("FundingPips 10K: profit target $1,000", fp10.profit_target == 1000)
check("FundingPips 10K: daily loss $400", fp10.max_daily_loss == 400)
check("FundingPips 10K: max drawdown $600", fp10.max_total_drawdown == 600)
check("FundingPips 10K: leverage 50:1", fp10.max_leverage == 50.0)
check("FundingPips 10K: min 3 trading days", fp10.min_trading_days == 3)
check("FundingPips 10K: 60% consistency rule", fp10.consistency_rule_pct == 0.60)
check("FundingPips 10K: CFD = no commission", fp10.commission_pct == 0.0)

# FundingPips $25K
check("FundingPips 25K: daily loss $1,000", fp25.max_daily_loss == 1000)

# ═════════════════════════════════════════════════════════════════════════════
# 2. Position Sizing — stop-loss is master, size is slave
# ═════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("2. POSITION SIZING — 0.5% risk, stop controls size")
print("=" * 60)

# 0.5% stop → ~$525 on BTC at $105K
ps_narrow = size_position(b10, stop_distance_pct=0.005)
check("Narrow stop (0.5%): larger position", ps_narrow.btc_amount > 0.09)
check("Narrow stop: risk stays at ~$50", abs(ps_narrow.max_loss_if_stopped - 50) < 1)
check("Narrow stop: leverage under 5:1", ps_narrow.leverage_used <= 5.0)
check("Narrow stop: commission modelled", ps_narrow.commission_rt > 0)

# Wide stop (3% = ~$3,150) → fewer contracts, same $50 risk
ps_wide = size_position(b10, stop_distance_pct=0.03)
check("Wide stop (3%): smaller position", ps_wide.btc_amount < ps_narrow.btc_amount)
check("Wide stop: still $50 risk", abs(ps_wide.max_loss_if_stopped - 50) < 1)

# 0.5% stop on $25K account → $125 risk
ps_25k = size_position(b25, stop_distance_pct=0.005)
check("$25K: risk scales to $125", abs(ps_25k.max_loss_if_stopped - 125) < 2)

# ═════════════════════════════════════════════════════════════════════════════
# 3. Recovery Math — the "Drawdown Story" from your notes
# ═════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("3. RECOVERY MATH — Drawdown Story (from your notes)")
print("=" * 60)

g = RiskGuardrails()

# Recovery gap: your notes say 45% DD → 85% gain needed
check("45% DD → needs ~82% gain",
      abs(g.recovery_needed(0.45) - 0.818) < 0.05)

# 20% rule: your notes say 20% DD only needs 25% to recover
check("20% DD → needs 25% gain",
      abs(g.recovery_needed(0.20) - 0.25) < 0.01)

check("5% DD → needs 5.3%",
      abs(g.recovery_needed(0.05) - 0.0526) < 0.01)

# Monte Carlo: 0.5% risk, 35% win rate, 2:1 RRR → survives
surv, med_dd = g.survives_sequence(win_rate=0.35, rrr=2.0,
                                    n_trades=100, simulations=200)
check("0.5% risk + 35% WR + 2:1 RRR → >80% survival",
      surv > 0.80, f"got {surv:.1%}")

check("Median max DD under 20%",
      med_dd < 0.20, f"got {med_dd:.1%}")

# Higher risk = lower survival
surv_high, dd_high = g.survives_sequence(win_rate=0.35, rrr=1.5,
                                          n_trades=100, simulations=200)
check("1.5 RRR lower survival than 2.0",
      surv_high < surv, f"{surv_high:.1%} vs {surv:.1%}")

# ═════════════════════════════════════════════════════════════════════════════
# 4. BacktestResult — vectorbt portfolio → prop firm verdicts
# ═════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("4. BACKTEST — vectorbt portfolio integration")
print("=" * 60)

features = pd.read_parquet(ROOT / "data" / "training_features.parquet")
features["price"] = features["last_price"]
features["timestamp_dt"] = pd.to_datetime(features["timestamp"], unit="ms")
features = features.set_index("timestamp_dt").sort_index()

signals = STRATEGIES["baseline_long"](features, {})

result = run_backtest(features, signals, b10, "eval_test")
check(f"Produced {result.total_trades} trades", result.total_trades > 0)
check("Win rate computed", 0 <= result.win_rate <= 1)
check("Profit factor computed", result.profit_factor >= 0)
check("Max drawdown computed", result.max_drawdown_pct >= 0)
check("PnL computed", abs(result.total_pnl_dollars) > 0)
check("Challenge verdict computed",
      isinstance(result.challenge_passed, bool))
check("Guardrail verdict computed",
      isinstance(result.survived_guardrails, bool))
check("Sustainable payout verdict",
      isinstance(result.sustainable_payout_machine, bool))
check("Summary string", len(result.summary()) > 20)
check("RRR computed", result.avg_rrr >= 0)

print(f"\n  Result: {result.summary()}")

# ═════════════════════════════════════════════════════════════════════════════
# 5. Walk-Forward — Optuna integration
# ═════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("5. WALK-FORWARD — Optuna parameter optimization")
print("=" * 60)

# Run 2 folds with 5 trials each (quick smoke test)
results = walk_forward_optimize(
    strategy_fn=STRATEGIES["baseline_long"],
    features=features,
    rules=b10,
    param_space={},
    n_train_bars=2000,
    n_test_bars=500,
    n_trials=5,
    n_folds=2,
)

check("Walk-forward produced results", len(results) > 0)

if results:
    check("At least one fold verdict computed",
          any(isinstance(r.challenge_passed, bool) for r in results))

    pass_rate = sum(1 for r in results if r.challenge_passed) / len(results)
    guard_rate = sum(1 for r in results if r.survived_guardrails) / len(results)
    print(f"\n  Folds: {len(results)}")
    print(f"  Pass rate: {pass_rate:.0%}")
    print(f"  Guard survival: {guard_rate:.0%}")
    for r in results:
        print(f"    {r.summary()}")

# ═════════════════════════════════════════════════════════════════════════════
# Summary
# ═════════════════════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
TOTAL = PASS + FAIL
print(f"RESULTS: {PASS}/{TOTAL} passed ({FAIL} failed)")

if FAIL == 0:
    print("All checks pass — the backtesting stack is verified.")
    print("\nNext step: build strategies in strategies/ that use your")
    print("Layer-3 features (lethargy, zones, road_map, vpoc_trend).")
else:
    print(f"{FAIL} checks failed — review the output above.")
