"""Fit exponential time constants to liquidity rise/drop regions and overlay them.

Model per region (x = days since the region's start):
    y = a * exp(-x / Texp) + b
  - a  : amplitude, sign free (a<0 -> saturating rise, a>0 -> decaying drop)
  - b  : constant shift, >= 0 (the asymptote liquidity approaches)
  - Texp: e-folding time constant in DAYS (what we're after)

Reads pool_liquidity.csv.xz (from fetch_pool_liquidity.py), fits each region with
scipy.optimize.curve_fit, prints the parameters, and plots the full liquidity
curve with the fitted curves drawn on top inside their date ranges.

Regions (defaults, editable via --regions or by editing REGIONS):
    rise: 2026-02-01 .. 2026-03-02  (exponential saturation)
    drop: 2026-05-03 .. 2026-06-10  (exponential drop)

Usage
-----
    uv run python fit_pool_liquidity.py
    uv run python fit_pool_liquidity.py --save pics/pool_liquidity_fit.png
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
DEFAULT_IN = HERE / "pool_liquidity.csv.xz"

# (label, start_date, end_date) — fitted independently.
REGIONS = [
    ("rise", "2026-02-01", "2026-03-02"),
    ("drop", "2026-05-03", "2026-06-10"),
]

DAY = 86400.0


def model(x, a, Texp, b):
    return a * np.exp(-x / Texp) + b


def to_ts(s: str) -> int:
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return int(d.timestamp())


def fit_region(ts, liq, t0, t1):
    """Fit y = a*exp(-x/T)+b over [t0, t1]. x in days from t0. Returns dict."""
    mask = (ts >= t0) & (ts <= t1)
    x = (ts[mask] - t0) / DAY            # days since region start
    y = liq[mask] / 1e6                  # $M
    if x.size < 4:
        raise SystemExit(f"region has too few points ({x.size})")

    # Initial guesses from the data shape.
    b0 = float(y[-1])                    # asymptote ~ last value
    a0 = float(y[0] - y[-1])             # signed amplitude
    span = float(x[-1] - x[0])
    T0 = max(span / 3.0, 1.0)

    popt, pcov = curve_fit(
        model, x, y, p0=[a0, T0, b0],
        bounds=([-np.inf, 1e-3, 0.0], [np.inf, np.inf, np.inf]),
        maxfev=20000,
    )
    a, Texp, b = popt
    perr = np.sqrt(np.diag(pcov))
    resid = y - model(x, *popt)
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return {
        "a": a, "Texp": Texp, "b": b,
        "a_err": perr[0], "Texp_err": perr[1], "b_err": perr[2],
        "r2": r2, "t0": t0, "x": x, "y": y,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", type=Path, default=DEFAULT_IN)
    ap.add_argument("--save", type=Path, default=None, help="save PNG instead of showing")
    args = ap.parse_args()

    if str(args.inp).endswith(".xz"):
        with lzma.open(args.inp) as f:
            df = pl.read_csv(f.read())
    else:
        df = pl.read_csv(args.inp)

    ts = df["timestamp"].to_numpy().astype(float)
    liq = df["liquidity"].to_numpy()

    fig, ax = plt.subplots(figsize=(13, 6))
    times = [dt.datetime.fromtimestamp(t, dt.UTC) for t in ts]
    ax.plot(times, liq / 1e6, lw=1.0, color="C0", label="liquidity", zorder=1)

    print(f"{'region':6} {'Texp (days)':>16} {'a ($M)':>14} {'b ($M)':>14} {'R^2':>7}")
    for i, (label, sd, ed) in enumerate(REGIONS):
        t0, t1 = to_ts(sd), to_ts(ed)
        r = fit_region(ts, liq, t0, t1)
        col = f"C{i + 1}"
        # Smooth fitted curve over the region.
        xx = np.linspace(r["x"][0], r["x"][-1], 400)
        tt = [dt.datetime.fromtimestamp(r["t0"] + xv * DAY, dt.UTC) for xv in xx]
        ax.plot(tt, model(xx, r["a"], r["Texp"], r["b"]),
                color=col, lw=2.5, zorder=3,
                label=f"{label}: T={r['Texp']:.1f}±{r['Texp_err']:.1f} d")
        ax.axvspan(dt.datetime.fromtimestamp(t0, dt.UTC),
                   dt.datetime.fromtimestamp(t1, dt.UTC),
                   color=col, alpha=0.07, zorder=0)
        print(f"{label:6} {r['Texp']:8.2f} ± {r['Texp_err']:5.2f} "
              f"{r['a']:8.2f}±{r['a_err']:5.2f} "
              f"{r['b']:8.2f}±{r['b_err']:5.2f} {r['r2']:7.4f}")

    ax.set_xlabel("date (UTC)")
    ax.set_ylabel("pool liquidity, $M  (balances0 + balances1)")
    ax.set_title("PYUSD/crvUSD pool liquidity — exponential fits")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()

    if args.save:
        fig.savefig(args.save, dpi=130)
        print(f"\nsaved -> {args.save}")
    else:
        plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
