"""Plot Aave v3 USDC supply APR over the long window from fetch_aave_usdc.py.

Reads aave_usdc_rates.csv.xz and plots supply APR (%) vs time, with an optional
rolling-median smoothing drawn over the faint raw line.

Usage
-----
    uv run python plot_aave_usdc.py
    uv run python plot_aave_usdc.py --smooth 15 --save pics/aave_usdc.png
"""
from __future__ import annotations

import argparse
import datetime as dt
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
DEFAULT_IN = HERE / "aave_usdc_rates.csv.xz"


def rolling_median(y: np.ndarray, w: int) -> np.ndarray:
    if w < 2:
        return y
    half = w // 2
    pad = np.pad(y, (half, half), mode="edge")
    return np.array([np.median(pad[i:i + w]) for i in range(len(y))])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", type=Path, default=DEFAULT_IN)
    ap.add_argument("--smooth", type=int, default=15,
                    help="rolling-median window (default 15; 0 disables)")
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()

    if str(args.inp).endswith(".xz"):
        with lzma.open(args.inp) as f:
            df = pl.read_csv(f.read())
    else:
        df = pl.read_csv(args.inp)

    ts = df["timestamp"].to_numpy().astype(float)
    times = [dt.datetime.fromtimestamp(t, dt.UTC) for t in ts]
    apr = df["aave_usdc_apr"].cast(pl.Float64, strict=False).to_numpy() * 100

    fig, ax = plt.subplots(figsize=(13, 6))
    if args.smooth >= 2:
        ax.plot(times, apr, lw=0.7, color="C0", alpha=0.3, label="supply APR (raw)")
        ax.plot(times, rolling_median(apr, args.smooth), lw=1.6, color="C0",
                label=f"supply APR (median-{args.smooth})")
    else:
        ax.plot(times, apr, lw=1.1, color="C0", label="supply APR")

    ax.set_xlabel("date (UTC)")
    ax.set_ylabel("APR, %")
    ax.set_title("Aave v3 USDC supply rate — 2024-01-01 .. now")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()

    if args.save:
        fig.savefig(args.save, dpi=130)
        print(f"saved -> {args.save}")
    else:
        plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
