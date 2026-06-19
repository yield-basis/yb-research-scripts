"""Response function: scrvUSD deposit flow vs rate-ratio (dead-band test).

Tests the hypothesis that scrvUSD depositors act as a dead-band negative feedback:
they do nothing while the scrvUSD rate is within ~[0.5x, 2x] of the market rate
(Sky savings rate), and only deposit/withdraw — with a ~1/e time constant — when
it leaves that band.

Construction:
  - state    x(t) = ln( r_scrvUSD(t) / m(t) ),  r = trailing-window scrvUSD APR,
             m = sUSDS Sky Savings Rate. The hypothesised dead band |x| < ln(2).
  - flow     phi(t) = d ln(N)/dt over the same trailing window, N = scrvUSD share
             supply (totalSupply). Shares move ONLY on deposit/withdraw, so this
             is the clean behavioral flow (per day).

The plot bins phi by x (mean +/- SEM per bin) over the faint raw scatter, marks
the hypothesised band edges x = +/-ln(2), and overlays a least-squares dead-band
fit. The fit estimates the actual band edges and the activation slopes, reported
as time constants tau = 1/slope (days):
    x > x_right (rate rich)  -> inflow,  tau_in  = 1/slope_right
    x < x_left  (rate poor)  -> outflow, tau_out = 1/|slope_left|
Compare tau_in ~ 11.4 d / tau_out ~ 5.9 d (the PYUSD pool constants).

Usage
-----
    uv run python plot_scrvusd_response.py
    uv run python plot_scrvusd_response.py --win 14 --lag 0 --save pics/scrvusd_response.png
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
SCRV_IN = HERE / "scrvusd_pps.csv.xz"
SUSDS_IN = HERE / "susds_rates.csv.xz"
YEAR = 365 * 86400
LN2 = np.log(2.0)


def read_xz(p: Path) -> pl.DataFrame:
    with lzma.open(p) as f:
        return pl.read_csv(f.read())


def parse_date(s: str) -> int:
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return int(d.timestamp())


def trailing_apr(ts: np.ndarray, pps: np.ndarray, win_days: float) -> np.ndarray:
    """Annualized pps growth over a trailing window (percent)."""
    w = win_days * 86400
    out = np.full(len(ts), np.nan)
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > w:
            j += 1
        if i > j and ts[i] > ts[j]:
            out[i] = (pps[i] / pps[j] - 1.0) / (ts[i] - ts[j]) * YEAR * 100
    return out


def trailing_dlnN(ts: np.ndarray, n: np.ndarray, win_days: float) -> np.ndarray:
    """d ln(N)/dt over a trailing window, per DAY."""
    w = win_days * 86400
    out = np.full(len(ts), np.nan)
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > w:
            j += 1
        if i > j and ts[i] > ts[j] and n[i] > 0 and n[j] > 0:
            out[i] = (np.log(n[i]) - np.log(n[j])) / ((ts[i] - ts[j]) / 86400.0)
    return out


def deadband(x, xl, xr, sl, sr):
    y = np.zeros_like(x, dtype=float)
    y = np.where(x < xl, sl * (x - xl), y)
    y = np.where(x > xr, sr * (x - xr), y)
    return y


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scrv", type=Path, default=SCRV_IN)
    ap.add_argument("--susds", type=Path, default=SUSDS_IN)
    ap.add_argument("--win", type=float, default=14.0,
                    help="trailing window (days) for both rate and flow (default 14)")
    ap.add_argument("--lag", type=float, default=0.0,
                    help="days the flow lags the rate (flow reacts to rate `lag` days "
                         "earlier); default 0")
    ap.add_argument("--bootstrap-end", default="2025-01-01",
                    help="exclude samples before this date (default 2025-01-01)")
    ap.add_argument("--bins", type=int, default=16, help="x bins (default 16)")
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()

    sd = read_xz(args.scrv).sort("timestamp")
    kd = read_xz(args.susds).sort("timestamp")

    sts = sd["timestamp"].to_numpy().astype(float)
    pps = sd["scrvusd_pps"].cast(pl.Float64, strict=False).to_numpy()
    nsh = sd["scrvusd_supply"].cast(pl.Float64, strict=False).to_numpy()
    kts = kd["timestamp"].to_numpy().astype(float)
    kapr = kd["susds_apr"].cast(pl.Float64, strict=False).to_numpy() * 100

    r = trailing_apr(sts, pps, args.win)            # scrvUSD APR, %
    m = np.interp(sts, kts, kapr)                   # market (Sky) APR, %
    phi = trailing_dlnN(sts, nsh, args.win)         # flow, per day

    # x = ln(r/m). Optionally evaluate the rate `lag` days before the flow.
    with np.errstate(divide="ignore", invalid="ignore"):
        x_all = np.log(r / m)
    if args.lag > 0:
        x_shift = np.interp(sts, sts + args.lag * 86400, x_all)  # rate from `lag`d ago
    else:
        x_shift = x_all

    boot = parse_date(args.bootstrap_end)
    good = (np.isfinite(x_shift) & np.isfinite(phi) & (r > 0) & (m > 0)
            & (sts >= boot))
    x = x_shift[good]
    y = phi[good]
    print(f"usable points: {good.sum()} (win={args.win:g}d, lag={args.lag:g}d, "
          f"post-bootstrap {args.bootstrap_end})")

    # --- binned mean +/- SEM ---
    lo, hi = np.percentile(x, [1, 99])
    edges = np.linspace(lo, hi, args.bins + 1)
    cx, my, ey = [], [], []
    for a, b in zip(edges[:-1], edges[1:]):
        sel = (x >= a) & (x < b)
        if sel.sum() >= 3:
            cx.append(0.5 * (a + b))
            my.append(y[sel].mean())
            ey.append(y[sel].std(ddof=1) / np.sqrt(sel.sum()))
    cx, my, ey = map(np.array, (cx, my, ey))

    # --- dead-band fit ---
    fit_txt = "fit failed"
    try:
        p0 = [-LN2, LN2, 0.02, 0.02]
        bounds = ([-2.0, 0.0, 0.0, 0.0], [0.0, 2.0, 1.0, 1.0])
        popt, _ = curve_fit(deadband, x, y, p0=p0, bounds=bounds, maxfev=20000)
        xl, xr, sl, sr = popt
        tau_out = 1.0 / sl if sl > 1e-9 else np.inf
        tau_in = 1.0 / sr if sr > 1e-9 else np.inf
        fit_txt = (f"band [{np.exp(xl):.2f}x, {np.exp(xr):.2f}x]   "
                   f"tau_in {tau_in:.1f}d  tau_out {tau_out:.1f}d")
        print(f"dead-band fit: x_left={xl:+.2f} (={np.exp(xl):.2f}x)  "
              f"x_right={xr:+.2f} (={np.exp(xr):.2f}x)")
        print(f"  tau_in (inflow) {tau_in:.1f} d   tau_out (outflow) {tau_out:.1f} d")
    except Exception as e:
        popt = None
        print("dead-band fit failed:", str(e)[:80])

    # --- plot ---
    fig, ax = plt.subplots(figsize=(11, 7))
    ax.axhline(0, color="0.6", lw=0.8)
    ax.scatter(x, y, s=8, color="0.6", alpha=0.25, label="samples")
    ax.errorbar(cx, my, yerr=ey, fmt="o-", color="C0", lw=1.5, ms=5,
                capsize=3, label="binned mean ± SEM")
    for xb, lbl in [(-LN2, "0.5×"), (LN2, "2×")]:
        ax.axvline(xb, color="C3", ls="--", lw=1.0)
        ax.text(xb, ax.get_ylim()[1], f" {lbl}", color="C3", va="top", fontsize=9)
    if popt is not None:
        xx = np.linspace(x.min(), x.max(), 400)
        ax.plot(xx, deadband(xx, *popt), color="C1", lw=2.0,
                label=f"dead-band fit: {fit_txt}")

    ax.set_xlabel("rate-ratio  x = ln( scrvUSD APR / Sky savings rate )")
    ax.set_ylabel("deposit flow  d ln(supply)/dt  [per day]")
    ax.set_title("scrvUSD deposit response vs rate-ratio "
                 f"(win {args.win:g}d, lag {args.lag:g}d)")
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
