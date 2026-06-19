"""crvUSD vs Sky: scrvUSD savings APR vs sUSDS (Sky Savings Rate).

Same construction as the Aave comparison (plot_crvusd_premium.py), but against
the Sky Savings Rate instead of Aave USDC supply. Both are stablecoin savings
rates, so the spread reflects how crvUSD's scrvUSD yield sits relative to Sky's.

Inputs:
  - scrvusd_pps.csv.xz  (fetch_scrvusd.py)  scrvUSD price-per-share
  - susds_rates.csv.xz  (fetch_susds.py)    Sky Savings Rate (ssr), as APR

scrvUSD APR is the realized yield over a trailing pps window (default 14 days),
robust to its weekly harvest steps. sUSDS APR (a governance-set step rate) is
interpolated by timestamp onto the scrvUSD samples; spread = scrvUSD − sUSDS.

Top panel: the two APR series. Bottom panel: the spread with its post-bootstrap
median marked (scrvUSD's launch had tiny TVL / erratic APR, so early samples are
excluded from the median — see --bootstrap-end).

Usage
-----
    uv run python plot_susds_premium.py
    uv run python plot_susds_premium.py --win 7 --save pics/susds_premium.png
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
SUSDS_IN = HERE / "susds_rates.csv.xz"
YEAR = 365 * 86400


def read_xz(p: Path) -> pl.DataFrame:
    with lzma.open(p) as f:
        return pl.read_csv(f.read())


def parse_date(s: str) -> int:
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return int(d.timestamp())


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
    ap.add_argument("--susds", type=Path, default=SUSDS_IN)
    ap.add_argument("--win", type=float, default=14.0,
                    help="trailing window (days) for scrvUSD APR (default 14)")
    ap.add_argument("--bootstrap-end", default="2025-01-01",
                    help="exclude samples before this date from the median (scrvUSD "
                         "launch had tiny TVL / erratic APR); default 2025-01-01")
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()

    sd = read_xz(args.scrv).sort("timestamp")
    kd = read_xz(args.susds).sort("timestamp")

    sts = sd["timestamp"].to_numpy().astype(float)
    pps = sd["scrvusd_pps"].cast(pl.Float64, strict=False).to_numpy()
    kts = kd["timestamp"].to_numpy().astype(float)
    kapr = kd["susds_apr"].cast(pl.Float64, strict=False).to_numpy() * 100

    scrv_apr = trailing_apr(sts, pps, args.win)
    susds_on_scrv = np.interp(sts, kts, kapr)   # align sUSDS onto scrvUSD samples
    spread = scrv_apr - susds_on_scrv

    boot_ts = parse_date(args.bootstrap_end)
    valid = ~np.isnan(spread)
    m = valid & (sts >= boot_ts)
    med = float(np.median(spread[m]))
    mean = float(np.mean(spread[m]))
    recent = m & (sts > sts[-1] - 90 * 86400)
    med_recent = float(np.median(spread[recent]))
    print(f"scrvUSD APR (trailing {args.win:g}d) vs sUSDS (Sky Savings Rate), "
          f"post-bootstrap {args.bootstrap_end} .. {sd['datetime_utc'][-1][:10]}")
    print(f"  spread median {med:+.2f}%  mean {mean:+.2f}%  last-90d median {med_recent:+.2f}%")

    times = [dt.datetime.fromtimestamp(t, dt.UTC) for t in sts]
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(13, 8), sharex=True, height_ratios=[2, 1])

    ax1.plot(times, scrv_apr, lw=1.4, color="C2", label=f"scrvUSD APR (trail {args.win:g}d)")
    ax1.plot(times, susds_on_scrv, lw=1.4, color="C1", label="sUSDS (Sky Savings Rate)")
    ax1.set_ylabel("APR, %")
    ax1.set_title("crvUSD vs Sky — scrvUSD savings vs sUSDS savings rate")
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc="upper right")

    boot_dt = dt.datetime.fromtimestamp(boot_ts, dt.UTC)
    for ax in (ax1, ax2):
        ax.axvspan(times[0], boot_dt, color="0.5", alpha=0.10)
    ax1.text(times[0], ax1.get_ylim()[1], " bootstrap (excluded)",
             va="top", ha="left", fontsize=8, color="0.4")

    ax2.axhline(0, color="0.6", lw=0.8)
    ax2.plot(times, spread, lw=1.2, color="C3", label="spread (scrvUSD − sUSDS)")
    ax2.axhline(med, color="C3", ls="--", lw=1.2,
                label=f"post-bootstrap median {med:+.2f}%")
    ax2.fill_between(times, spread, med, where=m, color="C3", alpha=0.12)
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
