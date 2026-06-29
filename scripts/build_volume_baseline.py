"""Build weekly volume baseline from historical trade data.

Reads trades from data/historical/trades_*.parquet (output of download_history.py),
aggregates volume per price bucket (same BUCKET=25 as core/history.py) per week,
computes mean + std deviation per bucket, and stores the result as
data/volume_baseline.parquet.

If no historical data is found, falls back to live-logger data (data/trades_*.parquet)
with an explicit warning.

Output schema (data/volume_baseline.parquet):
    price_bucket: int  (e.g. 50000 means bucket $50000-$50024)
    mean_volume:  float (mean weekly volume in this bucket)
    std_volume:   float (standard deviation of weekly volume)
    week_count:   int   (number of weeks used for the statistics)

Usage:
    python scripts/build_volume_baseline.py
"""

import sys
from pathlib import Path
import glob

import pandas as pd

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
HISTORICAL_DIR = DATA_DIR / "historical"
BASELINE_PATH = DATA_DIR / "volume_baseline.parquet"
BUCKET = 25


def _price_bucket(price: float) -> int:
    """Round price down to the nearest BUCKET-sized bucket."""
    return int(price / BUCKET) * BUCKET


def load_trades() -> pd.DataFrame:
    """Load trades from parquet files. Returns DataFrame with columns: timestamp, price, size, side."""
    # Priority 1: historical data
    hist_files = sorted(glob.glob(str(HISTORICAL_DIR / "trades_*.parquet")))
    if hist_files:
        print(f"Loading {len(hist_files)} historical trade files from {HISTORICAL_DIR}...")
        dfs = []
        for f in hist_files:
            df = pd.read_parquet(f)
            dfs.append(df)
        combined = pd.concat(dfs, ignore_index=True)
        print(f"  Loaded {len(combined):,} historical trades.")
        return combined

    # Priority 2: live-logger data (fallback)
    live_files = sorted(glob.glob(str(DATA_DIR / "trades_*.parquet")))
    if live_files:
        print(
            "WARNING: No historical data found in data/historical/. Using live-logger data from data/trades_*.parquet. "
            "Baseline will be based on limited data and may be less statistically robust.",
            file=sys.stderr,
        )
        dfs = []
        for f in live_files:
            df = pd.read_parquet(f)
            dfs.append(df)
        combined = pd.concat(dfs, ignore_index=True)
        print(f"  Loaded {len(combined):,} live-logger trades (fallback).")
        return combined

    raise FileNotFoundError(
        "No trade data found. Run scripts/download_history.py first, or let the logger agent collect live data."
    )


def build_baseline(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate volume per price bucket per week, compute mean + std."""
    # Ensure timestamp is in milliseconds and create week label
    if df["timestamp"].dtype in ("int64", "int32"):
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    else:
        df["datetime"] = pd.to_datetime(df["timestamp"])

    # Assign price bucket
    df["price_bucket"] = df["price"].apply(_price_bucket)

    # ISO week label
    df["week"] = df["datetime"].dt.strftime("%G-W%V")

    # Aggregate volume per bucket per week
    weekly = df.groupby(["price_bucket", "week"], observed=True)["size"].sum().reset_index()
    weekly.rename(columns={"size": "weekly_volume"}, inplace=True)

    print(f"  Aggregated {len(weekly)} bucket-week rows from {df['week'].nunique()} weeks.")

    # Compute statistics per bucket
    stats = (
        weekly.groupby("price_bucket", observed=True)["weekly_volume"]
        .agg(["mean", "std", "count"])
        .reset_index()
    )
    stats.rename(
        columns={"mean": "mean_volume", "std": "std_volume", "count": "week_count"},
        inplace=True,
    )

    # std may be NaN if only one week
    stats["std_volume"] = stats["std_volume"].fillna(0.0)

    print(f"  Computed baseline for {len(stats)} price buckets.")
    return stats


def main() -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("Loading trade data...")
    df = load_trades()
    print(f"  Columns: {list(df.columns)}")
    print(
        f"  Date range: {pd.to_datetime(df['timestamp'].min(), unit='ms')} "
        f"to {pd.to_datetime(df['timestamp'].max(), unit='ms')}"
    )

    baseline = build_baseline(df)
    baseline.to_parquet(BASELINE_PATH, index=False, compression="snappy")
    size_kb = BASELINE_PATH.stat().st_size / 1024
    print(f"Saved baseline to {BASELINE_PATH} ({size_kb:.1f} KB, {len(baseline)} buckets).")
    print("Done.")


if __name__ == "__main__":
    main()
