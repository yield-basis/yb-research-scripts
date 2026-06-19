"""Response function for the pyUSD/crvUSD pool (dead-band test), APR-driven.

Parallel to plot_scrvusd_response.py, but the "rate" is the pool's total LP APR
(fee + CRV + YB, average/boost-agnostic; YB gated by its campaign period_finish)
and the flow is the pool LP supply (minted/burned only on add/remove liquidity).

  state x(t) = ln( total_LP_APR(t) / market_rate(t) ),  market = sUSDS Sky rate
  flow  phi(t) = d ln(LP_supply)/dt over a trailing window  [per day]

The pool's APR swings far wider than scrvUSD's (YB campaigns drive it to tens of
percent), so excursions well outside the hypothesised |x|<ln(2) band are common —
a cleaner test of dead-band-then-activate behavior. Plot bins phi by x and fits a
dead-band model, reporting band edges and tau = 1/slope (days).

Usage
-----
    uv run python plot_pool_response.py
    uv run python plot_pool_response.py --win 14 --lag 0 --save pics/pool_response.png
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
from scipy.optimize import curve_fit

HERE = Path(__file__).resolve().parent
APR_IN = HERE / "pool_apr.csv.xz"
SUSDS_IN = HERE / "susds_rates.csv.xz"
YEAR = 365 * 86400
LN2 = np.log(2.0)


def read_xz(p):
    with lzma.open(p) as f:
        return pl.read_csv(f.read())


def parse_date(s):
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return int(d.timestamp())


def trailing(ts, x, win_days, kind):
    """kind='apr': annualized % growth of x; kind='dln': d ln(x)/dt per day."""
    w = win_days * 86400
    out = np.full(len(ts), np.nan)
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > w:
            j += 1
        if i > j and ts[i] > ts[j] and x[i] > 0 and x[j] > 0:
            if kind == "apr":
                out[i] = (x[i] / x[j] - 1.0) / (ts[i] - ts[j]) * YEAR * 100
            else:
                out[i] = (np.log(x[i]) - np.log(x[j])) / ((ts[i] - ts[j]) / 86400.0)
    return out


def deadband(x, xl, xr, sl, sr):
    y = np.zeros_like(x, dtype=float)
    y = np.where(x < xl, sl * (x - xl), y)
    y = np.where(x > xr, sr * (x - xr), y)
    return y


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apr", type=Path, default=APR_IN)
    ap.add_argument("--susds", type=Path, default=SUSDS_IN)
    ap.add_argument("--win", type=float, default=14.0,
                    help="trailing window (days) for fee APR and flow (default 14)")
    ap.add_argument("--lag", type=float, default=0.0,
                    help="days the flow lags the APR (default 0)")
    ap.add_argument("--flow", choices=["gauge", "pool"], default="gauge",
                    help="flow signal: gauge=staked TVL (excludes PegKeeper, default); "
                         "pool=total LP supply (includes single-sided PegKeeper)")
    ap.add_argument("--bins", type=int, default=16)
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()

    d = read_xz(args.apr).sort("timestamp")
    kd = read_xz(args.susds).sort("timestamp")
    g = lambda c: d[c].cast(pl.Float64, strict=False).to_numpy()
    ts = g("timestamp")
    vprice = g("virtual_price")
    gauge_staked = g("gauge_staked")
    gauge_tvl = gauge_staked * vprice
    # Flow signal: staked LP excludes the crvUSD PegKeeper (its single-sided
    # deposits are never staked), so it is the organic yield-seeking flow. The
    # pool totalSupply mixes in PegKeeper peg-defense activity.
    flow_src = gauge_staked if args.flow == "gauge" else g("lp_supply")
    crv = g("crv_rate") * g("crv_rel_weight") * g("crv_price") * YEAR / gauge_tvl * 100
    yb_on = ts < g("yb_period_finish")
    yb = np.where(yb_on, g("yb_rate") * g("yb_price") * YEAR / gauge_tvl * 100, 0.0)
    fee = trailing(ts, vprice, args.win, "apr")
    total_apr = np.nan_to_num(fee) + np.nan_to_num(crv) + np.nan_to_num(yb)

    kts = kd["timestamp"].to_numpy().astype(float)
    market = np.interp(ts, kts, kd["susds_apr"].cast(pl.Float64).to_numpy() * 100)

    phi = trailing(ts, flow_src, args.win, "dln")
    with np.errstate(divide="ignore", invalid="ignore"):
        x_all = np.log(total_apr / market)
    x_used = np.interp(ts, ts + args.lag * 86400, x_all) if args.lag > 0 else x_all

    good = np.isfinite(x_used) & np.isfinite(phi) & (total_apr > 0) & (market > 0)
    x, y = x_used[good], phi[good]
    print(f"usable points: {good.sum()}  (win={args.win:g}d, lag={args.lag:g}d)")
    print(f"x range [{x.min():.2f}, {x.max():.2f}]  "
          f"(APR/market {np.exp(x.min()):.2f}x .. {np.exp(x.max()):.1f}x)")

    lo, hi = np.percentile(x, [1, 99])
    edges = np.linspace(lo, hi, args.bins + 1)
    cx, my, ey = [], [], []
    for a, b in zip(edges[:-1], edges[1:]):
        sel = (x >= a) & (x < b)
        if sel.sum() >= 3:
            cx.append(0.5 * (a + b)); my.append(y[sel].mean())
            ey.append(y[sel].std(ddof=1) / np.sqrt(sel.sum()))
    cx, my, ey = map(np.array, (cx, my, ey))

    popt = None
    try:
        popt, _ = curve_fit(deadband, x, y, p0=[-LN2, LN2, 0.02, 0.02],
                            bounds=([-3, 0, 0, 0], [0, 3, 1, 1]), maxfev=20000)
        xl, xr, sl, sr = popt
        tin = 1 / sr if sr > 1e-9 else np.inf
        tout = 1 / sl if sl > 1e-9 else np.inf
        print(f"dead-band fit: band [{np.exp(xl):.2f}x, {np.exp(xr):.2f}x]  "
              f"tau_in {tin:.1f}d  tau_out {tout:.1f}d")
    except Exception as e:
        print("fit failed:", str(e)[:80])

    fig, ax = plt.subplots(figsize=(11, 7))
    ax.axhline(0, color="0.6", lw=0.8)
    ax.scatter(x, y, s=8, color="0.6", alpha=0.25, label="samples")
    ax.errorbar(cx, my, yerr=ey, fmt="o-", color="C0", lw=1.5, ms=5, capsize=3,
                label="binned mean ± SEM")
    for xb, lbl in [(-LN2, "0.5×"), (LN2, "2×")]:
        ax.axvline(xb, color="C3", ls="--", lw=1.0)
        ax.text(xb, ax.get_ylim()[1], f" {lbl}", color="C3", va="top", fontsize=9)
    if popt is not None:
        xx = np.linspace(x.min(), x.max(), 400)
        ax.plot(xx, deadband(xx, *popt), color="C1", lw=2.0,
                label=f"dead-band fit: [{np.exp(popt[0]):.2f}x,{np.exp(popt[1]):.2f}x] "
                      f"tau_in {1/popt[3]:.1f}d tau_out {1/popt[2]:.1f}d")

    ax.set_xlabel("rate-ratio  x = ln( pool total APR / Sky savings rate )")
    ax.set_ylabel(f"{args.flow}-staked flow  d ln(N)/dt  [per day]")
    ax.set_title("pyUSD/crvUSD LP deposit response vs APR-ratio "
                 f"({args.flow} flow, win {args.win:g}d, lag {args.lag:g}d)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=130)
        print(f"saved -> {args.save}")
    else:
        plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
