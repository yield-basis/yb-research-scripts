"""Plot monthly-average APR of the three market rates as a grouped bar chart.

Reads market_rates.csv.xz (from fetch_market_rates.py), buckets samples by
calendar month (UTC) and averages each series. Because samples are evenly spaced
in time, the per-month mean is a fair time-average APR (spikes included).

Series: LlamaLend WBTC borrow, scrvUSD savings, Aave v3 USDC supply.

Usage
-----
    uv run python plot_market_rates_monthly.py
    uv run python plot_market_rates_monthly.py --save pics/market_rates_monthly.png
"""
from __future__ import annotations

import argparse
import lzma
from pathlib import Path

import matplotlib
if "--save" in __import__("sys").argv:
    matplotlib.use("Agg")
else:
    try:
        matplotlib.use("QtAgg")
    except Exception:
        pass
import matplotlib.pyplot as plt
import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
DEFAULT_IN = HERE / "market_rates.csv.xz"

SERIES = [
    ("llama_apr", "LlamaLend WBTC borrow", "C3"),
    ("scrvusd_apr", "scrvUSD savings", "C2"),
    ("aave_usdc_apr", "Aave v3 USDC supply", "C0"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", type=Path, default=DEFAULT_IN)
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()

    if str(args.inp).endswith(".xz"):
        with lzma.open(args.inp) as f:
            df = pl.read_csv(f.read())
    else:
        df = pl.read_csv(args.inp)

    # Bucket by calendar month (UTC) and average each series (APR -> %).
    df = df.with_columns(
        pl.from_epoch("timestamp", time_unit="s").dt.truncate("1mo").alias("month")
    )
    cols = [c for c, _, _ in SERIES]
    monthly = (
        df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in cols])
        .group_by("month")
        .agg([(pl.col(c).mean() * 100).alias(c) for c in cols])
        .sort("month")
    )

    months = [m.strftime("%Y-%m") for m in monthly["month"].to_list()]
    x = np.arange(len(months))
    width = 0.26

    fig, ax = plt.subplots(figsize=(13, 6))
    for i, (col, label, color) in enumerate(SERIES):
        vals = monthly[col].to_numpy()
        bars = ax.bar(x + (i - 1) * width, vals, width, label=label, color=color)
        ax.bar_label(bars, fmt="%.1f", fontsize=8, padding=2)

    ax.set_xticks(x)
    ax.set_xticklabels(months)
    ax.set_xlabel("month (UTC)")
    ax.set_ylabel("average APR, %")
    ax.set_title("Monthly-average market rates — LlamaLend / scrvUSD / Aave USDC")
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()

    if args.save:
        fig.savefig(args.save, dpi=130)
        print(f"saved -> {args.save}")
    else:
        plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
