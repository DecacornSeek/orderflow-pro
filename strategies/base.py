"""Prop Firm Rules & Guardrails — the hard constraints every strategy must survive.

Encodes the rules from:
  - Breakout Prop (Classic 1-step): prop%20challenges/breakout_prop_btc_rules.html
  - FundingPips (1-step):          prop%20challenges/fundingpips_btc_prop_rules.html

PLUS the mathematical guardrails from prop firm scaling methodology:
  - 0.5% risk per trade (Drawdown Story)
  - RRR > 2:1 (Math of Strategy)
  - 20% max equity drawdown before strategy is abandoned (Recovery Gap)
  - Position sizing as slave to stop-loss distance
  - Commission + overnight swap modelled explicitly

All values are per BTC/USDT perpetuals at ~$105K BTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ═════════════════════════════════════════════════════════════════════════════
# 1. Prop Firm Rules — the hard contract each firm enforces
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PropFirmRules:
    """Immutable rule set for one prop firm account size + challenge type.

    Usage:
        breakout_10k = PropFirmRules.breakout_10k()
        breakout_25k = PropFirmRules.breakout_25k()
        fpips_10k    = PropFirmRules.fundingpips_10k()
        fpips_25k    = PropFirmRules.fundingpips_25k()
    """

    # -- Identity -----------------------------------------------------------
    firm: str                    # "breakout" | "fundingpips"
    account_size: float          # $10_000 | $25_000
    challenge_type: str          # "1-step" | "2-step-phase1" | etc.

    # -- Core constraints (all in dollars) -----------------------------------
    profit_target: float         # e.g. $1,000 (10% of $10K)
    max_daily_loss: float        # e.g. $300 (3%)
    max_total_drawdown: float    # e.g. $600 (6%)
    drawdown_type: str           # "static" | "trailing" | "balance"

    # -- Trading constraints -------------------------------------------------
    max_leverage: float          # 5.0 (Breakout) | 50.0 (FundingPips)
    commission_pct: float        # per side (e.g. 0.0004 = 0.04%)
    overnight_swap_pct: float    # per day (0.0 for CFD)
    min_trading_days: int        # 0 (Breakout) | 3 (FundingPips)
    consistency_rule_pct: float  # 0.0 = no rule, 0.60 = 60% max per trade

    # -- Payout --------------------------------------------------------------
    profit_split_pct: float      # 0.80 = 80% to trader

    # -- Instrument (BTC) ----------------------------------------------------
    btc_price_ref: float         # $105_000 — used for notional sizing
    contract_type: str           # "perpetual" | "cfd"

    # ------------------------------------------------------------------------
    # Factory methods — canonical configurations
    # ------------------------------------------------------------------------

    @classmethod
    def breakout_10k(cls) -> "PropFirmRules":
        return cls(
            firm="breakout", account_size=10_000, challenge_type="1-step",
            profit_target=1_000, max_daily_loss=300, max_total_drawdown=600,
            drawdown_type="static",
            max_leverage=5.0, commission_pct=0.0004, overnight_swap_pct=0.0009,
            min_trading_days=0, consistency_rule_pct=0.0,
            profit_split_pct=0.80, btc_price_ref=105_000, contract_type="perpetual",
        )

    @classmethod
    def breakout_25k(cls) -> "PropFirmRules":
        return cls(
            firm="breakout", account_size=25_000, challenge_type="1-step",
            profit_target=2_500, max_daily_loss=750, max_total_drawdown=1_500,
            drawdown_type="static",
            max_leverage=5.0, commission_pct=0.0004, overnight_swap_pct=0.0009,
            min_trading_days=0, consistency_rule_pct=0.0,
            profit_split_pct=0.80, btc_price_ref=105_000, contract_type="perpetual",
        )

    @classmethod
    def fundingpips_10k(cls) -> "PropFirmRules":
        return cls(
            firm="fundingpips", account_size=10_000, challenge_type="1-step",
            profit_target=1_000, max_daily_loss=400, max_total_drawdown=600,
            drawdown_type="balance",
            max_leverage=50.0, commission_pct=0.0, overnight_swap_pct=0.0,
            min_trading_days=3, consistency_rule_pct=0.60,
            profit_split_pct=0.80, btc_price_ref=105_000, contract_type="cfd",
        )

    @classmethod
    def fundingpips_25k(cls) -> "PropFirmRules":
        return cls(
            firm="fundingpips", account_size=25_000, challenge_type="1-step",
            profit_target=2_500, max_daily_loss=1_000, max_total_drawdown=1_500,
            drawdown_type="balance",
            max_leverage=50.0, commission_pct=0.0, overnight_swap_pct=0.0,
            min_trading_days=3, consistency_rule_pct=0.60,
            profit_split_pct=0.80, btc_price_ref=105_000, contract_type="cfd",
        )

    # ------------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------------

    @property
    def daily_loss_pct(self) -> float:
        return self.max_daily_loss / self.account_size

    @property
    def drawdown_pct(self) -> float:
        return self.max_total_drawdown / self.account_size

    @property
    def floor_price(self) -> float:
        """Account floor in dollars: starting_balance - max_total_drawdown."""
        return self.account_size - self.max_total_drawdown

    @property
    def commission_rt_pct(self) -> float:
        """Round-trip commission (both sides)."""
        return self.commission_pct * 2


# ═════════════════════════════════════════════════════════════════════════════
# 2. Mathematical Guardrails — the "Drawdown Story" from prop scaling notes
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RiskGuardrails:
    """Hard risk limits derived from prop firm mathematics.

    These are NOT per-firm — they are universal mathematical truths:
      - Recovery gap: 45% drawdown → needs 85% gain to break even
      - 20% rule: never exceed 20% equity drawdown
      - 0.5% risk per trade: the sweet spot that keeps sequences survivable
      - RRR > 2:1: profitable even at 40% win rate
    """

    risk_per_trade_pct: float     = 0.005   # 0.5% of account per trade
    max_equity_dd_pct: float      = 0.20    # abandon strategy if equity DD > 20%
    min_rrr: float                = 2.0     # minimum risk:reward ratio
    min_win_rate_at_min_rrr: float = 0.35   # 35% win rate breakeven at RRR=2.0
    max_consecutive_losses: int   = 10      # size so you survive this many losses
    trailing_stop_buffer_pct: float = 0.25  # extra 25% beyond logical stop level

    # -- Derived -------------------------------------------------------------

    @property
    def risk_per_trade_dollars_10k(self) -> float:
        """0.5% of $10K = $50 per trade."""
        return 10_000 * self.risk_per_trade_pct

    @property
    def risk_per_trade_dollars_25k(self) -> float:
        """0.5% of $25K = $125 per trade."""
        return 25_000 * self.risk_per_trade_pct

    @property
    def max_drawdown_dollars_10k(self) -> float:
        """20% of $10K = $2,000 — abandon point."""
        return 10_000 * self.max_equity_dd_pct

    @property
    def max_drawdown_dollars_25k(self) -> float:
        """20% of $25K = $5,000 — abandon point."""
        return 25_000 * self.max_equity_dd_pct

    def recovery_needed(self, drawdown_pct: float) -> float:
        """Gain needed to recover from a drawdown: 45% DD → 82% gain needed."""
        if drawdown_pct >= 1.0:
            return float("inf")
        return drawdown_pct / (1.0 - drawdown_pct)

    def survives_sequence(self, win_rate: float, rrr: float, n_trades: int = 100,
                          simulations: int = 500) -> Tuple[float, float]:
        """Monte Carlo: what % of sequences survive? Returns (survival_rate, median_max_dd)."""
        max_dds: List[float] = []
        survived = 0
        rng = np.random.default_rng(42)
        for _ in range(simulations):
            equity = 1.0
            peak = 1.0
            max_dd = 0.0
            for _ in range(n_trades):
                if rng.random() < win_rate:
                    equity *= (1.0 + rrr * self.risk_per_trade_pct)
                else:
                    equity *= (1.0 - self.risk_per_trade_pct)
                if equity > peak:
                    peak = equity
                dd = (peak - equity) / peak
                if dd > max_dd:
                    max_dd = dd
            max_dds.append(max_dd)
            if max_dd <= self.max_equity_dd_pct:
                survived += 1
        return survived / simulations, float(np.median(max_dds))


# ═════════════════════════════════════════════════════════════════════════════
# 3. Position Sizing — stop-loss is the master, position size is the slave
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PositionSize:
    """Result of sizing a trade to a given stop-loss distance."""

    btc_amount: float        # BTC contracts (e.g. 0.05)
    notional_usd: float      # notional value at current price
    leverage_used: float     # effective leverage
    max_loss_if_stopped: float  # $ loss if stop is hit
    commission_rt: float     # round-trip commission in $
    swap_per_day: float      # overnight cost per day
    risk_pct_of_account: float  # what % of account this trade risks


def size_position(
    rules: PropFirmRules,
    stop_distance_pct: float,     # e.g. 0.005 = 0.5% stop distance
    guardrails: RiskGuardrails = RiskGuardrails(),
    btc_price: Optional[float] = None,
) -> PositionSize:
    """Calculate position size such that hitting the stop = 0.5% account risk.

    Core principle from prop scaling notes:
    "Your position size is a slave to your stop-loss, not your desire for profit."

    If the stop is wide → fewer contracts. If the stop is tight → more contracts.
    Always the same dollar risk.

    Args:
        rules: PropFirmRules for the target firm + account.
        stop_distance_pct: Distance from entry to stop as % of price.
                           E.g. 0.005 = 0.5% stop = ~$525 on BTC at $105K.
        guardrails: RiskGuardrails (default: 0.5% risk per trade).
        btc_price: Current BTC price (default: rules.btc_price_ref).

    Returns:
        PositionSize with btc_amount, notional, leverage, and costs.
    """
    price = btc_price or rules.btc_price_ref
    risk_dollars = rules.account_size * guardrails.risk_per_trade_pct
    stop_dollars = price * stop_distance_pct

    if stop_dollars <= 0:
        raise ValueError(f"stop_distance_pct must be > 0, got {stop_distance_pct}")

    btc_amount = risk_dollars / stop_dollars
    notional = btc_amount * price
    leverage = notional / rules.account_size

    # Clamp to max leverage
    if leverage > rules.max_leverage:
        btc_amount = (rules.account_size * rules.max_leverage) / price
        notional = btc_amount * price
        leverage = rules.max_leverage
        # Recalculate actual risk
        actual_risk = btc_amount * stop_dollars
    else:
        actual_risk = risk_dollars

    commission_rt = notional * rules.commission_rt_pct
    swap_per_day = notional * rules.overnight_swap_pct

    return PositionSize(
        btc_amount=round(btc_amount, 4),
        notional_usd=round(notional, 2),
        leverage_used=round(leverage, 2),
        max_loss_if_stopped=round(actual_risk, 2),
        commission_rt=round(commission_rt, 2),
        swap_per_day=round(swap_per_day, 2),
        risk_pct_of_account=round(actual_risk / rules.account_size, 4),
    )


# ═════════════════════════════════════════════════════════════════════════════
# 4. BacktestResult — what every strategy run produces
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class BacktestResult:
    """Result of running one strategy against one rule set.

    This is the contract between strategy functions and the walk-forward
    engine. Every P3 strategy → BacktestResult.
    """

    # -- Strategy identity ---------------------------------------------------
    strategy_name: str
    rules: PropFirmRules
    params: Dict[str, float] = field(default_factory=dict)

    # -- Core metrics --------------------------------------------------------
    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0

    profit_factor: float = 0.0       # gross_profit / gross_loss
    total_pnl_dollars: float = 0.0
    total_pnl_pct: float = 0.0       # % of account
    avg_win_dollars: float = 0.0
    avg_loss_dollars: float = 0.0
    avg_rrr: float = 0.0             # avg_win / avg_loss

    # -- Risk metrics --------------------------------------------------------
    max_drawdown_dollars: float = 0.0
    max_drawdown_pct: float = 0.0
    max_daily_loss_dollars: float = 0.0
    max_daily_loss_pct: float = 0.0

    # -- Prop firm verdicts --------------------------------------------------
    profit_target_hit: bool = False
    daily_loss_breached: bool = False
    drawdown_breached: bool = False
    consistency_breached: bool = False
    min_days_met: bool = False

    # -- Derived verdict ────────────────────────────────────────────────────
    @property
    def challenge_passed(self) -> bool:
        """Did this run pass every prop firm constraint?"""
        return (
            self.profit_target_hit
            and not self.daily_loss_breached
            and not self.drawdown_breached
            and not self.consistency_breached
            and self.min_days_met
        )

    # -- Guardrail verdict (methodology math, not firm rules) ───────────────
    @property
    def survived_guardrails(self) -> bool:
        """Did equity drawdown stay under 20% (the Recovery Gap limit)?"""
        return self.max_drawdown_pct <= 0.20

    @property
    def rrr_sufficient(self) -> bool:
        """Is average RRR above 2.0 (profitable even at 35-40% win rate)?"""
        return self.avg_rrr >= 2.0

    @property
    def sustainable_payout_machine(self) -> bool:
        """All three: passed challenge + survived guardrails + positive expectancy."""
        return (
            self.challenge_passed
            and self.survived_guardrails
            and self.rrr_sufficient
            and self.profit_factor > 1.0
        )

    def summary(self) -> str:
        """One-line verdict usable by Hermes report generator."""
        verdict = "PASS" if self.sustainable_payout_machine else "FAIL"
        return (
            f"[{verdict}] {self.strategy_name} | {self.rules.firm} "
            f"${self.rules.account_size:,.0f} | "
            f"trades={self.total_trades} wr={self.win_rate:.0%} "
            f"pf={self.profit_factor:.2f} rrr={self.avg_rrr:.2f} "
            f"pnl={self.total_pnl_dollars:+,.0f} dd={self.max_drawdown_pct:.1%}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 5. Equity Curve Simulator — "Run 100 trades and see the truth"
# ═════════════════════════════════════════════════════════════════════════════

def simulate_equity_curve(
    rules: PropFirmRules,
    win_rate: float,
    rrr: float,
    n_trades: int = 100,
    guardrails: RiskGuardrails = RiskGuardrails(),
    seed: int = 42,
) -> pd.DataFrame:
    """Generate one possible equity curve given a win_rate and RRR.

    From prop scaling notes: "run at least 100 trades using your win probability
    and RRR to see the truth of your potential drawdowns."

    Returns:
        DataFrame with columns: trade, equity, pnl, drawdown_pct, daily_pnl,
        daily_loss_breach, drawdown_breach
    """
    rng = np.random.default_rng(seed)
    equity = rules.account_size
    daily_start = equity
    peak = equity
    data: List[Dict] = []

    # For consistency rule: track largest single profit
    single_trade_pnls: List[float] = []

    for i in range(1, n_trades + 1):
        risk = equity * guardrails.risk_per_trade_pct
        if rng.random() < win_rate:
            pnl = risk * rrr
        else:
            pnl = -risk

        equity += pnl
        single_trade_pnls.append(pnl)

        if equity > peak:
            peak = equity
        dd_pct = (peak - equity) / peak if peak > 0 else 0.0

        # Daily loss: track per calendar day (simplified: per-trade daily)
        daily_pnl = equity - daily_start
        daily_loss_breach = abs(daily_pnl) > rules.max_daily_loss

        # Drawdown breach
        dd_breach = (equity < rules.floor_price)

        data.append({
            "trade": i,
            "equity": round(equity, 2),
            "pnl": round(pnl, 2),
            "drawdown_pct": round(dd_pct, 4),
            "daily_pnl": round(daily_pnl, 2),
            "daily_loss_breach": daily_loss_breach,
            "drawdown_breach": dd_breach,
        })

    return pd.DataFrame(data)
