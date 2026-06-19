"""Per-campaign relaxation of staked TVL around YB incentive events.

The pool's YB campaigns switch on/off (and change rate) on a roughly weekly
cadence. After each step the *staked* TVL (gauge_staked × virtual_price — the
organic, PegKeeper-free liquidity) relaxes toward a new equilibrium. This script
detects the campaign events, fits

    TVL(t) = b + a · exp(-(t - t0) / tau)

to each inter-event segment, and overlays the fits on a TVL timeline (with the
total APR below). The exogenous YB step is the driver, so this sidesteps the
APR<->TVL simultaneity that made the instantaneous response function noisy.

Reports per-segment tau, split into inflow (TVL rising, after a rate rise) and
outflow (TVL falling). Note: segments are ~weekly, often shorter than tau, so
each tau is a partial-relaxation estimate; the medians across many segments are
the robust summary.

Usage
-----
    uv run python plot_campaign_response.py
    uv run python plot_campaign_response.py --min-amp 0.05 --save pics/campaign_response.png
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


def read_xz(p):
    with lzma.open(p) as f:
        return pl.read_csv(f.read())


def relax(t, a, tau, b):
    return b + a * np.exp(-t / tau)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apr", type=Path, default=APR_IN)
    ap.add_argument("--susds", type=Path, default=SUSDS_IN)
    ap.add_argument("--win", type=float, default=14.0, help="fee-APR window (days)")
    ap.add_argument("--min-amp", type=float, default=0.05,
                    help="min |d ln TVL| over a segment to fit it (default 0.05)")
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()

    d = read_xz(args.apr).sort("timestamp")
    g = lambda c: d[c].cast(pl.Float64, strict=False).to_numpy()
    ts = g("timestamp")
    vp = g("virtual_price")
    gs = g("gauge_staked")
    tvl = gs * vp / 1e6                                # staked TVL, $M
    gauge_tvl = gs * vp
    crv = g("crv_rate") * g("crv_rel_weight") * g("crv_price") * YEAR / gauge_tvl * 100
    ybrate, ybper, ybpx = g("yb_rate"), g("yb_period_finish"), g("yb_price")
    yb = np.where(ts < ybper, ybrate * ybpx * YEAR / gauge_tvl * 100, 0.0)

    def fee_apr():
        out = np.full(len(ts), np.nan); j = 0; W = args.win * 86400
        for i in range(len(ts)):
            while ts[i] - ts[j] > W:
                j += 1
            if i > j and vp[j] > 0:
                out[i] = (vp[i] / vp[j] - 1) / (ts[i] - ts[j]) * YEAR * 100
        return out
    total_apr = np.nan_to_num(fee_apr()) + np.nan_to_num(crv) + np.nan_to_num(yb)

    # --- detect campaign events (yb on/off, rate change, refund) ---
    yb_on = (ts < ybper).astype(int)
    ev = []
    for i in range(1, len(ts)):
        on_flip = yb_on[i] != yb_on[i - 1]
        rate_chg = ybrate[i - 1] > 0 and abs(ybrate[i] - ybrate[i - 1]) > 0.2 * ybrate[i - 1]
        per_jump = ybper[i] - ybper[i - 1] > 3 * 86400
        if on_flip or rate_chg or per_jump:
            ev.append(i)
    # cluster within 2 days
    events = []
    for i in ev:
        if events and ts[i] - ts[events[-1]] < 2 * 86400:
            continue
        events.append(i)

    # --- fit relaxation on each inter-event segment ---
    bounds_i = [events[k] for k in range(len(events))]
    segs = list(zip([0] + bounds_i, bounds_i + [len(ts) - 1]))
    fits = []
    for a_i, b_i in segs:
        if b_i - a_i < 4:
            continue
        tseg = (ts[a_i:b_i + 1] - ts[a_i]) / 86400.0
        yseg = tvl[a_i:b_i + 1]
        if tseg[-1] < 3.0:
            continue
        dln = np.log(yseg[-1] / yseg[0]) if yseg[0] > 0 else 0.0
        if abs(dln) < args.min_amp:
            continue
        try:
            p0 = [yseg[0] - yseg[-1], 5.0, yseg[-1]]
            popt, _ = curve_fit(relax, tseg, yseg, p0=p0,
                                bounds=([-1e4, 0.5, 0], [1e4, 60, 1e4]), maxfev=20000)
            a, tau, b = popt
            resid = yseg - relax(tseg, *popt)
            ss = 1 - np.sum(resid**2) / np.sum((yseg - yseg.mean())**2)
            fits.append((a_i, b_i, tau, dln, ss, popt))
        except Exception:
            pass

    tin = [f[2] for f in fits if f[3] > 0]
    tout = [f[2] for f in fits if f[3] < 0]
    print(f"{len(events)} events, {len(fits)} fitted segments "
          f"(|dlnTVL|>{args.min_amp})")
    print(f"  inflow  segments {len(tin)}  tau median {np.median(tin):.1f} d"
          if tin else "  inflow: none")
    print(f"  outflow segments {len(tout)}  tau median {np.median(tout):.1f} d"
          if tout else "  outflow: none")
    for a_i, b_i, tau, dln, ss, _ in fits:
        print(f"  {d['datetime_utc'][a_i][:10]} -> {d['datetime_utc'][b_i][:10]}  "
              f"{'IN ' if dln > 0 else 'OUT'}  tau {tau:5.1f} d  "
              f"dlnTVL {dln:+.2f}  R2 {ss:.2f}")

    # --- plot ---
    times = [dt.datetime.fromtimestamp(t, dt.UTC) for t in ts]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True,
                                   height_ratios=[2, 1])
    ax1.plot(times, tvl, lw=1.0, color="0.5", label="staked TVL ($M)")
    for a_i, b_i, tau, dln, ss, popt in fits:
        seg_t = ts[a_i:b_i + 1]
        seg_dt = [dt.datetime.fromtimestamp(t, dt.UTC) for t in seg_t]
        col = "C0" if dln > 0 else "C3"
        ax1.plot(seg_dt, relax((seg_t - seg_t[0]) / 86400.0, *popt),
                 color=col, lw=2.4)
        mid = seg_dt[len(seg_dt) // 2]
        ax1.annotate(f"{tau:.0f}d", (mid, relax((seg_t[len(seg_t)//2]-seg_t[0])/86400.0, *popt)),
                     fontsize=8, color=col, ha="center",
                     va="bottom" if dln > 0 else "top")
    for i in events:
        on = yb_on[i] > yb_on[i - 1]
        ax1.axvline(times[i], color="green" if on else "red", ls=":", lw=0.8, alpha=0.6)
    ax1.set_ylabel("staked TVL, $M")
    ax1.set_title("pyUSD/crvUSD staked-TVL relaxation around YB campaign events "
                  "(blue=inflow, red=outflow fits; green/red lines = YB on/off)")
    ax1.legend(loc="upper right")
    ax1.grid(True, alpha=0.3)

    ax2.plot(times, total_apr, lw=1.0, color="k", label="total LP APR")
    ax2.plot(times, yb, lw=0.9, color="C1", label="YB APR")
    ax2.set_ylabel("APR, %")
    ax2.set_xlabel("date (UTC)")
    ax2.legend(loc="upper right")
    ax2.grid(True, alpha=0.3)

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
