"""Plot typical market rates (APR %) vs time from fetch_market_rates.py.

Three series:
  - LlamaLend WBTC borrow APR   (AMM.rate())
  - scrvUSD savings APR         (derived from price-per-share growth; spiky at
                                 harvests, so a rolling-median smoothing is drawn
                                 over the faint raw line)
  - Aave v3 USDC supply APR     (currentLiquidityRate / 1e27)

The y-axis is clipped to a robust max (default: 98th percentile of all series,
×1.25) so utilization/harvest spikes don't flatten the rest; override with --ymax
or use --log.

Usage
-----
    uv run python plot_market_rates.py
    uv run python plot_market_rates.py --ymax 25 --smooth 9
    uv run python plot_market_rates.py --save pics/market_rates.png
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
DEFAULT_IN = HERE / "market_rates.csv.xz"


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
    ap.add_argument("--smooth", type=int, default=9,
                    help="rolling-median window for scrvUSD APR (default 9)")
    ap.add_argument("--ymax", type=float, default=None,
                    help="y-axis max in %% (default: robust auto)")
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()

    if str(args.inp).endswith(".xz"):
        with lzma.open(args.inp) as f:
            df = pl.read_csv(f.read())
    else:
        df = pl.read_csv(args.inp)

    ts = df["timestamp"].to_numpy().astype(float)
    times = [dt.datetime.fromtimestamp(t, dt.UTC) for t in ts]
    llama = df["llama_apr"].cast(pl.Float64, strict=False).to_numpy() * 100
    aave = df["aave_usdc_apr"].cast(pl.Float64, strict=False).to_numpy() * 100
    scrv = df["scrvusd_apr"].cast(pl.Float64, strict=False).to_numpy() * 100
    scrv_s = rolling_median(np.nan_to_num(scrv, nan=np.nanmedian(scrv)), args.smooth)

    fig, ax = plt.subplots(figsize=(13, 6))
    ax.plot(times, llama, lw=1.2, color="C3", label="LlamaLend WBTC borrow")
    ax.plot(times, aave, lw=1.2, color="C0", label="Aave v3 USDC supply")
    ax.plot(times, scrv, lw=0.6, color="C2", alpha=0.25)
    ax.plot(times, scrv_s, lw=1.4, color="C2",
            label=f"scrvUSD savings (median-{args.smooth})")

    ax.set_xlabel("date (UTC)")
    ax.set_ylabel("APR, %")
    ax.set_title("Typical market rates — LlamaLend / scrvUSD / Aave USDC")
    if args.log:
        ax.set_yscale("log")
    else:
        if args.ymax is not None:
            ymax = args.ymax
        else:
            allv = np.concatenate([llama, aave, scrv_s])
            allv = allv[np.isfinite(allv)]
            ymax = float(np.percentile(allv, 98)) * 1.25
        ax.set_ylim(0, ymax)
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
