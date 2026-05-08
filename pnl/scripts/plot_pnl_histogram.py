"""Histogram of per-user relative PnL (net_pnl_redem / avg_pos).

Reads pnl_all_users.csv produced by all_users_pnl.py and plots:
  - a console histogram via plotille (unicode-block bars)
  - a matplotlib PNG (pnl_histogram.png)

Each row in the CSV is one (market, user). The displayed metric is
unitless: how big the user's net PnL is relative to their time-weighted
average position size.

Usage:
    uv run python scripts/plot_pnl_histogram.py [CSV_PATH] [--low L --high H]
"""
from __future__ import annotations

import sys

import matplotlib
import numpy as np
import plotille
import polars as pl

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    low, high = -0.5, 1.5
    while "--low" in args or "--high" in args:
        if "--low" in args:
            i = args.index("--low")
            low = float(args[i + 1])
            args = args[:i] + args[i + 2:]
        if "--high" in args:
            i = args.index("--high")
            high = float(args[i + 1])
            args = args[:i] + args[i + 2:]
    csv_path = args[0] if args else "pnl_all_users.csv"

    df = pl.read_csv(csv_path)
    df = df.filter(pl.col("avg_pos") > 0)
    df = df.with_columns(rel=pl.col("net_pnl_redem") / pl.col("avg_pos"))
    df = df.filter(pl.col("rel").is_finite())

    inside = df.filter((pl.col("rel") >= low) & (pl.col("rel") <= high))
    clip_lo = (df["rel"] < low).sum()
    clip_hi = (df["rel"] > high).sum()
    vals = inside["rel"].to_list()

    print(f"CSV: {csv_path}  ({len(df)} non-trivial users)")
    print(f"\nDistribution: net_pnl_redem / avg_pos  (relative return per user)")
    print(plotille.hist(vals, bins=20, width=70, lc="cyan"))
    print(f"  median: {np.median(vals):+.3f}    mean: {np.mean(vals):+.3f}    std: {np.std(vals):.3f}")
    print(f"  inside [{low:+.2f}, {high:+.2f}]: {len(vals)};  "
          f"clipped: {clip_lo} below, {clip_hi} above")

    out_png = "pnl_histogram.png"
    plt.figure(figsize=(10, 5))
    plt.hist(vals, bins=20, range=(low, high), edgecolor="black", alpha=0.8)
    plt.axvline(0, color="red", linestyle="--", linewidth=1, label="0")
    plt.axvline(np.median(vals), color="green", linestyle=":", linewidth=1,
                label=f"median={np.median(vals):+.3f}")
    plt.xlabel("net_pnl_redem / avg_pos (unitless return)")
    plt.ylabel("# users")
    plt.title(f"YB markets: per-user PnL relative to avg position size (n={len(vals)})")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(out_png, dpi=120)
    print(f"\nSaved {out_png}")


if __name__ == "__main__":
    main()
