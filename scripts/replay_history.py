"""
Replay historischer Trades → Trainings-Features generieren.

Was passiert:
  1. Lädt alle data/historical/trades_YYYY-MM-DD.parquet Dateien
  2. Replayed jeden Trade durch CVD + Imbalance Logik (minutenweise)
  3. Generiert Features pro Minute: imbalance, rolling_delta, cvd_ratio, etc.
  4. Labelt jeden Punkt mit zukünftigem Preis: +5min, +15min, +30min
  5. Speichert: data/training_features.parquet

Verwendung:
  python scripts/replay_history.py
  python scripts/replay_history.py --days 7   # nur letzte 7 Tage

Training-Feature Schema:
  timestamp, last_price, volume_1m, buy_volume_1m, sell_volume_1m,
  rolling_delta, cumulative_delta, cvd_ratio, buy_ratio,
  price_5m, price_15m, price_30m,
  direction_5m, direction_15m, direction_30m   (1=up, 0=down)
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# Damit core importierbar ist
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.cvd import CVD

HIST_DIR = Path(__file__).parent.parent / "data" / "historical"
OUT_PATH = Path(__file__).parent.parent / "data" / "training_features.parquet"


def replay_day(df: pd.DataFrame) -> pd.DataFrame:
    """Einen Tag durch CVD replayed und Minuten-Features generieren."""
    cvd = CVD(window_size=200)

    df = df.sort_values("timestamp").reset_index(drop=True)
    df["minute"] = (df["timestamp"] // 60_000) * 60_000

    features = []
    for minute, group in df.groupby("minute"):
        for _, row in group.iterrows():
            cvd.update(float(row["price"]), float(row["size"]), row["side"])

        snap   = cvd.snapshot()
        rb = snap.get("rolling_buy_volume", 0) or 0
        rs = snap.get("rolling_sell_volume", 0) or 0
        tr = rb + rs
        cvd_ratio = (rb - rs) / tr if tr > 0 else 0.0
        volume = group["size"].sum()
        buy_v  = group.loc[group["side"] == "buy",  "size"].sum()
        sell_v = group.loc[group["side"] == "sell", "size"].sum()

        features.append({
            "timestamp":        minute,
            "last_price":       float(group["price"].iloc[-1]),
            "volume_1m":        round(volume, 6),
            "buy_volume_1m":    round(buy_v, 6),
            "sell_volume_1m":   round(sell_v, 6),
            "buy_ratio":        round(buy_v / volume, 4) if volume > 0 else 0.5,
            "rolling_delta":    snap["rolling_delta"],
            "cumulative_delta": snap["cumulative_delta"],
            "cvd_ratio":        round(cvd_ratio, 4),
            "trade_count":      snap["trade_count"],
        })

    return pd.DataFrame(features)


def add_labels(df: pd.DataFrame) -> pd.DataFrame:
    """Zukünftige Preise als Labels hinzufügen (Shift über sortierte Zeitreihe)."""
    df = df.sort_values("timestamp").reset_index(drop=True)

    for minutes in [5, 15, 30]:
        df[f"price_{minutes}m"] = df["last_price"].shift(-minutes)
        df[f"direction_{minutes}m"] = (
            df[f"price_{minutes}m"] > df["last_price"]
        ).astype("Int8")

    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Historische Trades zu Trainings-Features verarbeiten")
    parser.add_argument("--days", type=int, default=None, help="Nur letzte N Tage verarbeiten")
    args = parser.parse_args()

    files = sorted(HIST_DIR.glob("trades_*.parquet"))
    if not files:
        print(f"Keine Dateien in {HIST_DIR}")
        print("Erst ausführen: python scripts/download_history.py")
        sys.exit(1)

    if args.days:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=args.days)
        files = [f for f in files if f.stem.replace("trades_", "") >= cutoff.strftime("%Y-%m-%d")]

    print(f"Verarbeite {len(files)} Tage...")

    all_features = []
    for f in files:
        date = f.stem.replace("trades_", "")
        print(f"  {date} ...", end=" ", flush=True)
        df = pd.read_parquet(f)
        day_features = replay_day(df)
        all_features.append(day_features)
        print(f"{len(day_features)} Minuten")

    if not all_features:
        print("Keine Features generiert.")
        sys.exit(1)

    combined = pd.concat(all_features, ignore_index=True)
    combined = combined.sort_values("timestamp").reset_index(drop=True)
    combined = add_labels(combined)

    # Letzte 30/15/5 Minuten haben keine Labels → droppen
    combined = combined.dropna(subset=["price_5m"])

    OUT_PATH.parent.mkdir(exist_ok=True)
    combined.to_parquet(OUT_PATH, index=False, compression="snappy")

    size_mb = OUT_PATH.stat().st_size / 1_048_576
    print(f"\nFertig!")
    print(f"  {len(combined):,} Trainings-Samples")
    print(f"  {OUT_PATH} ({size_mb:.1f} MB)")
    print(f"\nFeatures: {list(combined.columns)}")
    print(f"\nBeispiel-Zeile:")
    print(combined.iloc[0].to_string())

    # Kurze Statistik
    for minutes in [5, 15, 30]:
        col = f"direction_{minutes}m"
        if col in combined.columns:
            up_pct = combined[col].mean() * 100
            print(f"\n  +{minutes}min: {up_pct:.1f}% up / {100-up_pct:.1f}% down")


if __name__ == "__main__":
    main()
