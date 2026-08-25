"""
validate_pattern_engine.py - Backtest validation of PatternEngine signal quality.

Replays 30 days of historical trades against the volume baseline, computes
z-scores and absorption flags per minute (vectorised — no per-trade Python loop),
joins against training_features.parquet direction labels, and prints a report.

Data dependencies:
  data/historical/trades_YYYY-MM-DD.parquet   (from download_history.py)
  data/volume_baseline.parquet                (from build_volume_baseline.py)
  data/training_features.parquet              (from replay_history.py)

Usage:
  python scripts/validate_pattern_engine.py
  python scripts/validate_pattern_engine.py --z-threshold 3.0 --days 7
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

HIST_DIR            = ROOT / "data" / "historical"
FEATURES_PATH       = ROOT / "data" / "training_features.parquet"
BASELINE_PATH       = ROOT / "data" / "volume_baseline.parquet"
VALIDATION_OUT_PATH = ROOT / "data" / "pattern_validation.parquet"
BUCKET              = 25
MIN_WEEKS           = 2


# ---------------------------------------------------------------------------
# Core: replay one day vectorised (avoids 107M-row iterrows)
# ---------------------------------------------------------------------------

def _replay_day(
    df: pd.DataFrame,
    baseline: pd.DataFrame,
    z_threshold: float,
    absorption_ratio: float,
) -> pd.DataFrame:
    """Return a DataFrame of (price_bucket, minute, z_score, absorption) hits.

    Fully vectorised via groupby + cumsum — no Python-level trade loop.
    Session VAP is implicitly reset because cumsum starts fresh for each call.
    """
    df = df[["timestamp", "price", "size", "side"]].copy()
    df["minute"]       = (df["timestamp"] // 60_000) * 60_000
    df["price_bucket"] = (df["price"] / BUCKET).astype(int) * BUCKET
    df["delta"]        = df["size"] * df["side"].map({"buy": 1.0, "sell": -1.0}).fillna(-1.0)
    df = df.sort_values("timestamp")

    # Cumulative volume per (price_bucket, minute) — session-cumulative within the day
    mb = (
        df.groupby(["price_bucket", "minute"], observed=True)
        .agg(vol=("size", "sum"), delta=("delta", "sum"))
        .reset_index()
        .sort_values(["price_bucket", "minute"])
    )
    mb["cum_vol"] = mb.groupby("price_bucket", observed=True)["vol"].cumsum()

    # Per-minute absorption proxy: |net_delta| / total_vol
    ms = (
        df.groupby("minute", observed=True)
        .agg(total_vol=("size", "sum"), total_delta=("delta", "sum"))
        .reset_index()
    )
    ms["absorption"] = (ms["total_delta"].abs() / ms["total_vol"]).clip(0, 1) < absorption_ratio

    # Join baseline stats + compute z-score
    result = mb.merge(
        baseline[["price_bucket", "mean_volume", "std_volume", "week_count"]],
        on="price_bucket", how="inner",
    ).merge(ms[["minute", "absorption"]], on="minute")

    result["z_score"] = (result["cum_vol"] - result["mean_volume"]) / result["std_volume"]

    return result[
        (result["std_volume"] > 0)
        & (result["week_count"] >= MIN_WEEKS)
        & (result["z_score"].abs() >= z_threshold)
    ][["price_bucket", "minute", "z_score", "cum_vol", "absorption"]].copy()


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def _row(df: pd.DataFrame, label: str, base: Dict[int, float]) -> None:
    n = len(df)
    if n == 0:
        print(f"  {label:<36s}  n=    0")
        return
    parts = []
    for h in [5, 15, 30]:
        col = f"direction_{h}m"
        acc = float(df[col].mean())
        parts.append(f"+{h}m {acc*100:5.1f}% ({(acc-base[h])*100:+.1f}pp)")
    print(f"  {label:<36s}  n={n:5,}  |  {'  '.join(parts)}")


def print_report(merged: pd.DataFrame, features: pd.DataFrame, n_days: int) -> None:
    base = {h: float(features[f"direction_{h}m"].mean()) for h in [5, 15, 30]}

    sep = "=" * 76
    print(f"\n{sep}")
    print("  PATTERN ENGINE VALIDATION REPORT")
    print(sep)
    print(f"  Days replayed: {n_days}  |  Labelled minutes: {len(features):,}")
    print(f"  pp = percentage-point delta vs. baseline\n")

    print(f"  {'Segment':<36s}  {'Count':>7}  |  Accuracy (vs. baseline)")
    print(f"  {'-'*72}")

    _row(features.dropna(subset=["direction_5m"]), "Baseline (all minutes)", base)
    _row(merged, "All pattern hits", base)
    _row(merged[merged["absorption"] == True],  "  absorption = True", base)
    _row(merged[merged["absorption"] == False], "  absorption = False", base)

    print()
    for lo, hi in [(2.0, 3.0), (3.0, 5.0), (5.0, 10.0), (10.0, 1e9)]:
        sub = merged[(merged["z_score"].abs() >= lo) & (merged["z_score"].abs() < hi)]
        label = f"  z [{lo:.0f}-{hi:.0f})" if hi < 1e9 else f"  z >= {lo:.0f}"
        _row(sub, label, base)

    print(f"\n  Z-score distribution of all hits:")
    for lo, hi, lbl in [(2, 3, "2-3"), (3, 5, "3-5"), (5, 10, "5-10"), (10, 1e9, "10+")]:
        n = int(((merged["z_score"].abs() >= lo) & (merged["z_score"].abs() < hi)).sum())
        bar = "#" * min(40, n * 40 // max(1, len(merged)))
        print(f"    z {lbl:<6}  {n:6,}  {bar}")

    print(f"\n{sep}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=None,
                        help="Only replay the last N days")
    parser.add_argument("--z-threshold", type=float, default=2.0)
    parser.add_argument("--absorption-ratio", type=float, default=0.15)
    args = parser.parse_args()

    for path, hint in [
        (BASELINE_PATH,  "python scripts/build_volume_baseline.py"),
        (FEATURES_PATH,  "python scripts/replay_history.py"),
    ]:
        if not path.exists():
            print(f"ERROR: {path} not found. Run: {hint}")
            sys.exit(1)

    hist_files = sorted(HIST_DIR.glob("trades_*.parquet"))
    if not hist_files:
        print(f"ERROR: No historical files in {HIST_DIR}")
        sys.exit(1)

    if args.days:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=args.days)).strftime("%Y-%m-%d")
        hist_files = [f for f in hist_files if f.stem.replace("trades_", "") >= cutoff]

    baseline = pd.read_parquet(BASELINE_PATH)
    features = pd.read_parquet(FEATURES_PATH)
    features = features.dropna(subset=["direction_5m", "direction_15m", "direction_30m"])

    print(f"Baseline:  {len(baseline):,} price buckets")
    print(f"Features:  {len(features):,} labelled minutes")
    print(f"Files:     {len(hist_files)} days  |  z >= {args.z_threshold}  |  absorption < {args.absorption_ratio}\n")

    all_hits: List[pd.DataFrame] = []
    for f in hist_files:
        date = f.stem.replace("trades_", "")
        df   = pd.read_parquet(f)
        hits = _replay_day(df, baseline, args.z_threshold, args.absorption_ratio)
        all_hits.append(hits)
        print(f"  {date}  {len(hits):5,} hits", flush=True)

    hits_df = pd.concat(all_hits, ignore_index=True) if all_hits else pd.DataFrame()

    if hits_df.empty:
        print("\nNo pattern hits — try lowering --z-threshold.")
        sys.exit(0)

    # One signal per minute: keep highest |z_score|
    hits_df = (
        hits_df.reindex(hits_df["z_score"].abs().sort_values(ascending=False).index)
        .drop_duplicates("minute")
        .reset_index(drop=True)
    )
    print(f"\nUnique-minute hits: {len(hits_df):,}  "
          f"(absorption: {hits_df['absorption'].sum():,} = {hits_df['absorption'].mean()*100:.1f}%)")

    # Join labels
    merged = hits_df.merge(
        features[["timestamp", "direction_5m", "direction_15m", "direction_30m"]],
        left_on="minute", right_on="timestamp", how="inner",
    ).drop(columns="timestamp")
    print(f"Matched to labels:  {len(merged):,} ({len(merged)/len(hits_df)*100:.0f}% of hits)")

    if merged.empty:
        print("No label matches — check timestamp alignment.")
        sys.exit(1)

    VALIDATION_OUT_PATH.parent.mkdir(exist_ok=True)
    merged.to_parquet(VALIDATION_OUT_PATH, index=False, compression="snappy")
    print(f"Saved: {VALIDATION_OUT_PATH}")

    print_report(merged, features, n_days=len(hist_files))


if __name__ == "__main__":
    main()
