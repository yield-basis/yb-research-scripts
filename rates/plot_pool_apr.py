"""Derive and plot the pyUSD/crvUSD LP APR components over time.

From pool_apr.csv.xz (fetch_pool_apr.py), build the average (boost-agnostic) APR:

  fee APR  = annualized trailing virtual_price growth
  CRV APR  = crv_rate × crv_rel_weight × CRV_price × yr / gauge_TVL
  YB  APR  = yb_rate × YB_price × yr / gauge_TVL,  ONLY while timestamp <
             yb_period_finish (Curve gauges keep the last rate after a campaign
             ends, so YB income is gated by period_finish — it switches on/off)
  total    = fee + CRV + YB

gauge_TVL = gauge_staked × virtual_price (LP ≈ $1). Plots the three components
(stacked-ish as lines) and the total; the YB term dominates and toggles with its
incentive campaigns.

Usage
-----
    uv run python plot_pool_apr.py
    uv run python plot_pool_apr.py --win 14 --save pics/pool_apr.png
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
DEFAULT_IN = HERE / "pool_apr.csv.xz"
YEAR = 365 * 86400


def trailing_apr(ts, x, win_days):
    """Annualized growth of x over a trailing window (percent)."""
    w = win_days * 86400
    out = np.full(len(ts), np.nan)
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > w:
            j += 1
        if i > j and ts[i] > ts[j] and x[j] > 0:
            out[i] = (x[i] / x[j] - 1.0) / (ts[i] - ts[j]) * YEAR * 100
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", type=Path, default=DEFAULT_IN)
    ap.add_argument("--win", type=float, default=14.0,
                    help="trailing window (days) for the fee APR (default 14)")
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()

    with lzma.open(args.inp) as f:
        df = pl.read_csv(f.read())
    g = lambda c: df[c].cast(pl.Float64, strict=False).to_numpy()
    ts = g("timestamp")
    vprice = g("virtual_price")
    gauge_tvl = g("gauge_staked") * vprice           # ≈ USD
    crv_rate, rw, crv_px = g("crv_rate"), g("crv_rel_weight"), g("crv_price")
    yb_rate, yb_px = g("yb_rate"), g("yb_price")
    yb_period = g("yb_period_finish")

    with np.errstate(divide="ignore", invalid="ignore"):
        fee = trailing_apr(ts, vprice, args.win)
        crv = crv_rate * rw * crv_px * YEAR / gauge_tvl * 100
        yb_on = ts < yb_period                        # gate: campaign active
        yb = np.where(yb_on, yb_rate * yb_px * YEAR / gauge_tvl * 100, 0.0)
    total = np.nan_to_num(fee) + np.nan_to_num(crv) + np.nan_to_num(yb)

    frac_on = float(np.mean(yb_on))
    print(f"YB active {100*frac_on:.0f}% of samples")
    for nm, a in [("fee", fee), ("CRV", crv), ("YB", yb), ("total", total)]:
        v = a[np.isfinite(a)]
        print(f"  {nm:5} APR  median {np.median(v):6.2f}%  max {np.max(v):7.2f}%")

    times = [dt.datetime.fromtimestamp(t, dt.UTC) for t in ts]
    fig, ax = plt.subplots(figsize=(13, 6))
    ax.plot(times, total, lw=1.8, color="k", label="total LP APR")
    ax.plot(times, yb, lw=1.1, color="C1", label="YB incentive APR")
    ax.plot(times, crv, lw=1.1, color="C0", label="CRV gauge APR (avg)")
    ax.plot(times, fee, lw=1.1, color="C2", label=f"fee APR (trail {args.win:g}d)")
    ax.set_xlabel("date (UTC)")
    ax.set_ylabel("APR, %")
    ax.set_title("pyUSD/crvUSD LP APR components (average / boost-agnostic)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
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
