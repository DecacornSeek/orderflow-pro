"""
Download historischer BTC/USDT Trade-Daten von Binance.

Quelle: https://data.binance.vision/data/spot/daily/trades/BTCUSDT/
Format: BTCUSDT-trades-YYYY-MM-DD.zip -> CSV mit:
  id, price, qty, quoteQty, time, isBuyerMaker, isBestMatch

Verwendung:
  python scripts/download_history.py            # letzte 30 Tage
  python scripts/download_history.py --days 90  # letzte 90 Tage

Speichert als: data/historical/trades_YYYY-MM-DD.parquet
"""

import argparse
import io
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

BASE_URL  = "https://data.binance.vision/data/spot/daily/trades/BTCUSDT"
DATA_DIR  = Path(__file__).parent.parent / "data" / "historical"
COLUMNS   = ["id", "price", "qty", "quoteQty", "time", "isBuyerMaker", "isBestMatch"]


def download_day(date: str) -> pd.DataFrame | None:
    """Einen Tag herunterladen und als DataFrame zurückgeben."""
    url  = f"{BASE_URL}/BTCUSDT-trades-{date}.zip"
    path = DATA_DIR / f"trades_{date}.parquet"

    if path.exists():
        print(f"  {date} — bereits vorhanden, überspringe.")
        return None

    try:
        resp = requests.get(url, timeout=60, stream=True)
        if resp.status_code == 404:
            print(f"  {date} — nicht verfügbar (noch nicht hochgeladen).")
            return None
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  {date} — Download Fehler: {e}")
        return None

    # ZIP im Speicher entpacken
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        csv_name = z.namelist()[0]
        with z.open(csv_name) as f:
            df = pd.read_csv(f, header=None, names=COLUMNS)

    # Nur relevante Spalten behalten + Typen setzen
    df = df[["time", "price", "qty", "isBuyerMaker"]].copy()
    df.columns = ["timestamp", "price", "size", "isBuyerMaker"]
    df["timestamp"]    = (df["timestamp"] // 1_000).astype("int64")  # µs -> ms
    df["price"]        = df["price"].astype(float)
    df["size"]         = df["size"].astype(float)
    df["side"]         = df["isBuyerMaker"].map({True: "sell", False: "buy"})
    df["exchange"]     = "binance"
    df = df[["timestamp", "price", "size", "side", "exchange"]]

    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, compression="snappy")
    trades = len(df)
    size_mb = path.stat().st_size / 1_048_576
    print(f"  {date} — {trades:,} Trades -> {size_mb:.1f} MB")
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance historische Trade-Daten herunterladen")
    parser.add_argument("--days", type=int, default=30, help="Anzahl Tage (default: 30)")
    parser.add_argument("--start", type=str, help="Startdatum YYYY-MM-DD (überschreibt --days)")
    args = parser.parse_args()

    today = datetime.now(timezone.utc).date()

    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
    else:
        start = today - timedelta(days=args.days)

    # Gestern ist der letzte verfügbare Tag (heute noch nicht hochgeladen)
    end = today - timedelta(days=1)

    dates = []
    d = start
    while d <= end:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    print(f"Lade {len(dates)} Tage BTC/USDT Trade-Daten von Binance...")
    print(f"  Zeitraum: {dates[0]} bis {dates[-1]}")
    print(f"  Ziel: {DATA_DIR}\n")

    total_trades = 0
    for date in dates:
        df = download_day(date)
        if df is not None:
            total_trades += len(df)

    print(f"\nFertig. {total_trades:,} neue Trades gespeichert.")
    print(f"Nächster Schritt: python scripts/replay_history.py")


if __name__ == "__main__":
    main()
