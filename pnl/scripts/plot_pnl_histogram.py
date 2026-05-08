"""Histogram of per-user relative PnL (net_pnl_redem / avg_pos), in matplotlib.

Reads pnl_all_users.csv produced by all_users_pnl.py. The displayed metric
is unitless: how big the user's net PnL is relative to their time-weighted
average position size. NB: this is *absolute* (not annualized) return over
each user's individual holding period.

Usage:
    uv run python scripts/plot_pnl_histogram.py [CSV_PATH] [--low L --high H]
"""
from __future__ import annotations

import os
import sys

import matplotlib

# QtAgg via pyqt6 — single pip dep with bundled Qt binaries, no system
# rebuild needed. The system default GTK4Cairo backend would need both
# pycairo and PyGObject and a working GTK4 stack.
matplotlib.use("QtAgg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402


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
    vals = inside["rel"].to_numpy()
    median = float(np.median(vals))
    mean = float(np.mean(vals))

    print(f"CSV: {csv_path}  (non-trivial users: {len(df)}, "
          f"shown: {len(vals)}, clipped: {clip_lo} below / {clip_hi} above)")
    print(f"  median: {median:+.1%}    mean: {mean:+.1%}    std: {np.std(vals):.1%}")

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.hist(vals, bins=20, range=(low, high), edgecolor="black", alpha=0.85)
    ax.axvline(0, color="red", linestyle="--", linewidth=1, label="0%")
    ax.axvline(median, color="green", linestyle=":", linewidth=1.5,
               label=f"median = {median:+.1%}")
    ax.axvline(mean, color="orange", linestyle=":", linewidth=1.5,
               label=f"mean = {mean:+.1%}")
    ax.xaxis.set_major_formatter(mticker.PercentFormatter(xmax=1, decimals=0))
    ax.set_xlabel("Net PnL as % of average position size  (absolute, not annualized)")
    ax.set_ylabel("# users")
    ax.set_title(
        f"YB markets 3–6: per-user PnL distribution  "
        f"(n={len(vals)}; clipped {clip_lo}<{low:+.0%}, {clip_hi}>{high:+.0%})"
    )
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()

    fig.savefig("pnl_histogram.png", dpi=120)
    print("Saved pnl_histogram.png")

    plt.show()


if __name__ == "__main__":
    main()
