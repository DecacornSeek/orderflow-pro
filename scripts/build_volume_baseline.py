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


def _stream_aggregate(files: list) -> pd.DataFrame:
    """Aggregate volume per (price_bucket, week) one file at a time to avoid OOM.

    With 107M trades at ~30 bytes/row, loading all at once = ~3.2 GB RAM.
    Streaming and reducing per file keeps peak usage to one file (~100 MB).
    """
    bucket_week: dict = {}
    total_trades = 0

    for f in files:
        df = pd.read_parquet(f, columns=["timestamp", "price", "size"])
        df["price_bucket"] = (df["price"] / BUCKET).astype(int) * BUCKET
        df["week"] = pd.to_datetime(df["timestamp"], unit="ms").dt.strftime("%G-W%V")
        for (bucket, week), vol in df.groupby(["price_bucket", "week"])["size"].sum().items():
            key = (int(bucket), week)
            bucket_week[key] = bucket_week.get(key, 0.0) + float(vol)
        total_trades += len(df)
        del df

    print(f"  Streamed {total_trades:,} trades -> {len(bucket_week)} bucket-week pairs.")
    return pd.DataFrame(
        [{"price_bucket": b, "week": w, "weekly_volume": v} for (b, w), v in bucket_week.items()]
    )


def build_baseline() -> pd.DataFrame:
    """Build volume baseline from historical (or fallback live) trade files."""
    hist_files = sorted(glob.glob(str(HISTORICAL_DIR / "trades_*.parquet")))
    if hist_files:
        print(f"Streaming {len(hist_files)} historical files from {HISTORICAL_DIR}...")
        weekly = _stream_aggregate(hist_files)
    else:
        live_files = sorted(glob.glob(str(DATA_DIR / "trades_*.parquet")))
        if not live_files:
            raise FileNotFoundError(
                "No trade data found. Run scripts/download_history.py first, "
                "or let the logger agent collect live data."
            )
        print(
            "WARNING: No historical data found. Using live-logger data (less robust).",
            file=sys.stderr,
        )
        print(f"Streaming {len(live_files)} live files from {DATA_DIR}...")
        weekly = _stream_aggregate(live_files)

    stats = (
        weekly.groupby("price_bucket", observed=True)["weekly_volume"]
        .agg(["mean", "std", "count"])
        .reset_index()
    )
    stats.rename(
        columns={"mean": "mean_volume", "std": "std_volume", "count": "week_count"},
        inplace=True,
    )
    stats["std_volume"] = stats["std_volume"].fillna(0.0)
    print(f"  Computed baseline for {len(stats)} price buckets.")
    return stats


def main() -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("Building volume baseline...")
    baseline = build_baseline()
    baseline.to_parquet(BASELINE_PATH, index=False, compression="snappy")
    size_kb = BASELINE_PATH.stat().st_size / 1024
    print(f"Saved baseline to {BASELINE_PATH} ({size_kb:.1f} KB, {len(baseline)} buckets).")
    print("Done.")


if __name__ == "__main__":
    main()
