"""crvUSD risk premium: scrvUSD savings APR vs Aave v3 USDC supply APR.

Idea: deposits flow into scrvUSD until its yield settles toward a market rate;
because crvUSD carries extra risk vs USDC, scrvUSD should sit above the USDC
lending rate by a risk premium. This script measures that spread over scrvUSD's
full life (since 2024-10-31).

Inputs:
  - scrvusd_pps.csv.xz     (fetch_scrvusd.py)    price-per-share
  - aave_usdc_rates.csv.xz (fetch_aave_usdc.py)  USDC supply APR

scrvUSD APR is the realized yield over a trailing pps window (default 14 days),
robust to the vault's weekly harvest steps. Aave APR is interpolated by timestamp
onto the scrvUSD samples, and spread = scrvUSD_APR − Aave_APR.

Top panel: the two APR series. Bottom panel: the spread, with its median marked.

Usage
-----
    uv run python plot_crvusd_premium.py
    uv run python plot_crvusd_premium.py --win 7 --save pics/crvusd_premium.png
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
SCRV_IN = HERE / "scrvusd_pps.csv.xz"
AAVE_IN = HERE / "aave_usdc_rates.csv.xz"
YEAR = 365 * 86400


def read_xz(p: Path) -> pl.DataFrame:
    with lzma.open(p) as f:
        return pl.read_csv(f.read())


def trailing_apr(ts: np.ndarray, pps: np.ndarray, win_days: float) -> np.ndarray:
    """Annualized pps growth over a trailing window of win_days (percent)."""
    w = win_days * 86400
    out = np.full(len(ts), np.nan)
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > w:
            j += 1
        if i > j and ts[i] > ts[j]:
            out[i] = (pps[i] / pps[j] - 1.0) / (ts[i] - ts[j]) * YEAR * 100
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scrv", type=Path, default=SCRV_IN)
    ap.add_argument("--aave", type=Path, default=AAVE_IN)
    ap.add_argument("--win", type=float, default=14.0,
                    help="trailing window (days) for scrvUSD APR (default 14)")
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()

    sd = read_xz(args.scrv).sort("timestamp")
    ad = read_xz(args.aave).sort("timestamp")

    sts = sd["timestamp"].to_numpy().astype(float)
    pps = sd["scrvusd_pps"].cast(pl.Float64, strict=False).to_numpy()
    ats = ad["timestamp"].to_numpy().astype(float)
    aapr = ad["aave_usdc_apr"].cast(pl.Float64, strict=False).to_numpy() * 100

    scrv_apr = trailing_apr(sts, pps, args.win)
    aave_on_scrv = np.interp(sts, ats, aapr)   # align Aave onto scrvUSD samples
    spread = scrv_apr - aave_on_scrv

    m = ~np.isnan(spread)
    med = float(np.median(spread[m]))
    mean = float(np.mean(spread[m]))
    # recent (last 90d) spread for contrast
    recent = m & (sts > sts[-1] - 90 * 86400)
    med_recent = float(np.median(spread[recent]))
    print(f"scrvUSD APR (trailing {args.win:g}d) vs Aave USDC supply, "
          f"{sd['datetime_utc'][0][:10]} .. {sd['datetime_utc'][-1][:10]}")
    print(f"  spread median {med:+.2f}%  mean {mean:+.2f}%  last-90d median {med_recent:+.2f}%")

    times = [dt.datetime.fromtimestamp(t, dt.UTC) for t in sts]
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(13, 8), sharex=True, height_ratios=[2, 1])

    ax1.plot(times, scrv_apr, lw=1.4, color="C2", label=f"scrvUSD APR (trail {args.win:g}d)")
    ax1.plot(times, aave_on_scrv, lw=1.4, color="C0", label="Aave v3 USDC supply APR")
    ax1.set_ylabel("APR, %")
    ax1.set_title("crvUSD risk premium — scrvUSD savings vs Aave USDC supply")
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc="upper right")

    ax2.axhline(0, color="0.6", lw=0.8)
    ax2.plot(times, spread, lw=1.2, color="C3", label="spread (scrvUSD − Aave)")
    ax2.axhline(med, color="C3", ls="--", lw=1.2, label=f"median {med:+.2f}%")
    ax2.fill_between(times, spread, med, where=~np.isnan(spread), color="C3", alpha=0.12)
    ax2.set_ylabel("spread, %")
    ax2.set_xlabel("date (UTC)")
    ax2.grid(True, alpha=0.3)
    ax2.legend(loc="upper right")

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
