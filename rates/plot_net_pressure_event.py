#!/usr/bin/env python3
"""Price tracking (p_cex vs price_scale) and the 2024-08-05 net_pressure spike.

The largest net_pressure excursions in the simulated data (>50%) all cluster on
2024-08-05, the BTC "Black Monday" crash. This script:

  * plots p_cex (real CEX price) vs price_scale (pool's internal scale) over the
    full simulation, plus a zoom on the crash window with net_pressure overlaid;
  * prints how long net_pressure stayed above a set of thresholds during the
    event (the persistence that matters for sizing a slow incentive controller).

net_pressure is computed exactly as in net_pressure.py (factor of 2, p_cex
denominator).

Usage
-----
    uv run python plot_net_pressure_event.py
    uv run python plot_net_pressure_event.py --save pics/net_pressure_event.png
"""
import argparse
import datetime as dt
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

from net_pressure import load_npz_xz, PRICE_KEY

HERE = Path(__file__).resolve().parent
DEFAULT = ("btc-candidates-yb-opt/"
           "btc_a5_mf120_of163_fg00850937_don0187374_rpf433333/detailed-output.npz.xz")
# A different parameter set that barely suffered the event (max ~+33%), for contrast.
COMPARE = ("btc-candidates-yb-opt/"
           "btc_a5_mf146_of170_fg0542027_don004091_rpf301010/dust600/detailed-output.npz.xz")
# Crash window to zoom into (UTC).
ZOOM_LO = dt.datetime(2024, 8, 3, tzinfo=dt.timezone.utc)
ZOOM_HI = dt.datetime(2024, 8, 8, tzinfo=dt.timezone.utc)
THRESHOLDS = [0.20, 0.30, 0.40, 0.50]


def compute(path):
    d = load_npz_xz(path)
    t = d["t"].astype(np.int64)
    token0 = d["token0"].astype(np.float64)
    token1 = d["token1"].astype(np.float64)
    price_scale = d["price_scale"].astype(np.float64)
    price = d[PRICE_KEY].astype(np.float64)
    debt = (token0 + token1 * price_scale) / 2.0
    tvl = token0 + token1 * price
    with np.errstate(divide="ignore", invalid="ignore"):
        npr = 2.0 * (debt - token0) / tvl
    ok = np.isfinite(npr) & (tvl > 0)
    return t[ok], price[ok], price_scale[ok], npr[ok]


def report_event(t, npr, label=""):
    lo, hi = int(ZOOM_LO.timestamp()), int(ZOOM_HI.timestamp())
    m = (t >= lo) & (t < hi)
    tw, nw = t[m], npr[m]
    order = np.argsort(tw)
    tw, nw = tw[order], nw[order]
    dtw = np.diff(tw, prepend=tw[0])  # seconds represented by each sample
    print(f"event window {ZOOM_LO:%Y-%m-%d} .. {ZOOM_HI:%Y-%m-%d}  "
          f"({m.sum():,} samples)  [{label}]")
    peak_i = int(np.argmax(nw))
    print(f"  peak net_pressure : {nw[peak_i] * 100:+.2f}%  at "
          f"{dt.datetime.fromtimestamp(int(tw[peak_i]), dt.timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    for thr in THRESHOLDS:
        sel = nw > thr
        hours = dtw[sel].sum() / 3600.0
        if sel.any():
            first = dt.datetime.fromtimestamp(int(tw[sel][0]), dt.timezone.utc)
            last = dt.datetime.fromtimestamp(int(tw[sel][-1]), dt.timezone.utc)
            print(f"  net_pressure > {thr:.0%} : {hours:5.1f} h cumulative   "
                  f"({first:%m-%d %H:%M} .. {last:%m-%d %H:%M} UTC)")
        else:
            print(f"  net_pressure > {thr:.0%} : none")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=DEFAULT, help="detailed-output.npz.xz")
    ap.add_argument("--compare", default=COMPARE,
                    help="second candidate to overlay for contrast (or '' to skip)")
    ap.add_argument("--save", metavar="PNG", help="save figure instead of showing")
    args = ap.parse_args()

    name_main = Path(args.path).parent.name
    t, price, price_scale, npr = compute(args.path)
    report_event(t, npr, label=name_main)

    cmp_data = None
    if args.compare:
        name_cmp = Path(args.compare).parent.name
        tc, _, _, nprc = compute(args.compare)
        report_event(tc, nprc, label=name_cmp)
        cmp_data = (tc.astype("datetime64[s]"), nprc, name_cmp)

    tt = t.astype("datetime64[s]")

    fig, (axTop, axBot) = plt.subplots(2, 1, figsize=(13, 9))

    # --- Top: full-range price tracking (downsampled for speed) ---
    step = max(1, t.size // 6000)
    axTop.plot(tt[::step], price[::step], lw=0.7, color="black", label="p_cex (real)")
    axTop.plot(tt[::step], price_scale[::step], lw=0.7, color="darkorange",
               alpha=0.8, label="price_scale (pool)")
    axTop.axvspan(np.datetime64(ZOOM_LO.replace(tzinfo=None)),
                  np.datetime64(ZOOM_HI.replace(tzinfo=None)),
                  color="red", alpha=0.08, label="zoom window")
    axTop.set_title("p_cex vs price_scale — full simulation")
    axTop.set_ylabel("BTC price [$]")
    axTop.legend(loc="upper left")
    axTop.grid(alpha=0.3)

    # --- Bottom: crash-window zoom, price + net_pressure twin axis ---
    lo64 = np.datetime64(ZOOM_LO.replace(tzinfo=None))
    hi64 = np.datetime64(ZOOM_HI.replace(tzinfo=None))
    m = (tt >= lo64) & (tt < hi64)
    axBot.plot(tt[m], price[m], lw=0.9, color="black", label="p_cex (real)")
    axBot.plot(tt[m], price_scale[m], lw=0.9, color="darkorange", alpha=0.85,
               label="price_scale (pool)")
    axBot.set_ylabel("BTC price [$]")
    axBot.set_title(f"Zoom: {ZOOM_LO:%Y-%m-%d} crash — price & net_pressure")
    axBot.grid(alpha=0.3)

    axNp = axBot.twinx()
    axNp.plot(tt[m], npr[m] * 100, lw=0.9, color="steelblue", alpha=0.85,
              label=f"net_pressure ({name_main})")
    if cmp_data is not None:
        ttc, nprc, name_cmp = cmp_data
        mc = (ttc >= lo64) & (ttc < hi64)
        axNp.plot(ttc[mc], nprc[mc] * 100, lw=0.9, color="seagreen", alpha=0.85,
                  label=f"net_pressure ({name_cmp})")
    axNp.axhline(50, color="crimson", ls="--", lw=1.0, label="50%")
    axNp.set_ylabel("net_pressure [%]", color="steelblue")
    axNp.tick_params(axis="y", labelcolor="steelblue")

    # merge legends
    h1, l1 = axBot.get_legend_handles_labels()
    h2, l2 = axNp.get_legend_handles_labels()
    axBot.legend(h1 + h2, l1 + l2, loc="upper right")

    fig.tight_layout()
    if args.save:
        out = Path(args.save)
        fig.savefig(out, dpi=120)
        print(f"saved {out}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
