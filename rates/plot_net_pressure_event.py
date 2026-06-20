#!/usr/bin/env python3
"""Price tracking (p_cex vs price_scale) and the 2024-08-05 net_pressure spike.

The largest net_pressure excursions in the simulated data (>50%) all cluster on
2024-08-05, the BTC "Black Monday" crash. This script:

  * plots p_cex (real CEX price) vs price_scale (pool's internal scale) over the
    full simulation, plus one zoom panel per parameter set on the crash window
    (each with its own price + net_pressure axes, so curves don't overlap);
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
ROOT = "btc-candidates-yb-opt"
DEFAULT = ("btc-candidates-yb-opt/"
           "btc_a5_mf120_of163_fg00850937_don0187374_rpf433333/detailed-output.npz.xz")
# A different parameter set that barely suffered the event (max ~+33%), for contrast.
COMPARE = ("btc-candidates-yb-opt/"
           "btc_a5_mf146_of170_fg0542027_don004091_rpf301010/dust600/detailed-output.npz.xz")
# Crash window to zoom into (UTC).
ZOOM_LO = dt.datetime(2024, 8, 3, tzinfo=dt.timezone.utc)
ZOOM_HI = dt.datetime(2024, 8, 8, tzinfo=dt.timezone.utc)
THRESHOLDS = [0.20, 0.30, 0.40, 0.50]


def label_of(path):
    """Full parameter-set identifier, e.g. 'btc_a5_mf146_..._rpf301010/dust600'."""
    import os
    return os.path.relpath(os.path.dirname(path), ROOT)


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
    ap.add_argument("paths", nargs="*", default=[DEFAULT, COMPARE],
                    help="detailed-output.npz.xz files, one zoom panel each "
                         "(default: worst mf120_of163 + tame mf146_of170/dust600)")
    ap.add_argument("--save", metavar="PNG", help="save figure instead of showing")
    args = ap.parse_args()

    cands = []  # (label, t, price, price_scale, npr)
    for p in args.paths:
        label = label_of(p)
        t, price, price_scale, npr = compute(p)
        report_event(t, npr, label=label)
        cands.append((label, t, price, price_scale, npr))

    lo64 = np.datetime64(ZOOM_LO.replace(tzinfo=None))
    hi64 = np.datetime64(ZOOM_HI.replace(tzinfo=None))

    nrows = 1 + len(cands)  # full-range panel + one zoom panel per candidate
    fig, axes = plt.subplots(nrows, 1, figsize=(13, 3.6 * nrows))
    axTop = axes[0]

    # --- Top: full-range price tracking (p_cex is shared; price_scale from cand 0) ---
    lbl0, t0, price0, ps0, _ = cands[0]
    tt0 = t0.astype("datetime64[s]")
    step = max(1, t0.size // 6000)
    axTop.plot(tt0[::step], price0[::step], lw=0.7, color="black", label="p_cex (real)")
    axTop.plot(tt0[::step], ps0[::step], lw=0.7, color="darkorange", alpha=0.8,
               label=f"price_scale ({lbl0})")
    axTop.axvspan(lo64, hi64, color="red", alpha=0.08, label="zoom window")
    axTop.set_title("p_cex vs price_scale — full simulation")
    axTop.set_ylabel("BTC price [$]")
    axTop.legend(loc="upper left", fontsize=8)
    axTop.grid(alpha=0.3)

    # --- One zoom panel per parameter set ---
    np_colors = ["steelblue", "seagreen", "purple", "teal"]
    for i, (label, t, price, price_scale, npr) in enumerate(cands):
        ax = axes[i + 1]
        tt = t.astype("datetime64[s]")
        m = (tt >= lo64) & (tt < hi64)
        ax.plot(tt[m], price[m], lw=0.9, color="black", label="p_cex (real)")
        ax.plot(tt[m], price_scale[m], lw=0.9, color="darkorange", alpha=0.85,
                label="price_scale")
        ax.set_ylabel("BTC price [$]")
        ax.set_title(f"{ZOOM_LO:%Y-%m-%d} crash — {label}")
        ax.grid(alpha=0.3)
        ax.set_xlim(lo64, hi64)

        axNp = ax.twinx()
        c = np_colors[i % len(np_colors)]
        axNp.plot(tt[m], npr[m] * 100, lw=1.0, color=c, alpha=0.9, label="net_pressure")
        axNp.axhline(50, color="crimson", ls="--", lw=1.0, label="50%")
        axNp.set_ylim(-20, 60)  # shared scale across panels for fair comparison
        axNp.set_ylabel("net_pressure [%]", color=c)
        axNp.tick_params(axis="y", labelcolor=c)

        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = axNp.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, loc="upper right", fontsize=8)

    fig.tight_layout()
    if args.save:
        out = Path(args.save)
        fig.savefig(out, dpi=120)
        print(f"saved {out}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
