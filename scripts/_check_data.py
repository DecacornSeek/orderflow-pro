import sys
from pathlib import Path
# Pruefe beide moeglichen Pfade
candidates = [
    Path("C:/Users/ericl/orderlfow-pro/data"),
    Path("C:/Users/ericl/OneDrive/MiroFish/orderflow-pro/data"),
    Path("../orderlfow-pro/data"),
]
for c in candidates:
    if c.exists():
        print(f"EXISTS: {c}")
        for f in sorted(c.glob("*.parquet"))[:10]:
            print(f"  {f.name} ({f.stat().st_size / 1_048_576:.1f} MB)")
        hist = c / "historical"
        if hist.exists():
            hfiles = sorted(hist.glob("*.parquet"))
            print(f"  historical/: {len(hfiles)} files")
            for f in hfiles[:3]:
                print(f"    {f.name} ({f.stat().st_size / 1_048_576:.1f} MB)")
            if hfiles:
                import pandas as pd
                df = pd.read_parquet(hfiles[0])
                print(f"    Schema: {list(df.columns)}")
                print(f"    Rows: {len(df):,}")
    else:
        print(f"MISSING: {c}")
