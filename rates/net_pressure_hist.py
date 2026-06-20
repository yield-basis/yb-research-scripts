#!/usr/bin/env python3
"""Histogram of net_pressure pooled across all simulated pool dumps.

net_pressure is computed exactly as in net_pressure.py (factor of 2, p_cex in the
denominator). The distribution is sharply peaked near zero with occasional large
excursions, so we show two panels:

  left  : full range, log-y (reveals the tails / large-pressure events)
  right : zoomed to +/-10%, linear-y (reveals the bulk shape near zero)
"""
import argparse
import glob

import numpy as np

import matplotlib

from net_pressure import load_npz_xz, net_pressure


def collect(paths):
    chunks = []
    for path in paths:
        chunks.append(net_pressure(load_npz_xz(path)))
    return np.concatenate(chunks)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*",
                    default=sorted(glob.glob("btc-candidates-yb-opt/**/detailed-output.npz.xz",
                                             recursive=True)),
                    help="detailed-output.npz.xz files (default: all candidates)")
    ap.add_argument("--bins", type=int, default=400, help="number of bins (default 400)")
    ap.add_argument("--out", default="pics/net_pressure_hist.png", help="output PNG path")
    ap.add_argument("--save-only", action="store_true",
                    help="save PNG without opening an interactive window")
    args = ap.parse_args()

    if not args.paths:
        ap.error("no detailed-output.npz.xz files found")

    matplotlib.use("Agg" if args.save_only else "QtAgg")
    import matplotlib.pyplot as plt

    np_ = collect(args.paths) * 100.0  # to percent
    n = np_.size
    mean = np_.mean()
    pos = np_[np_ > 0]

    print(f"pooled points : {n:,}")
    print(f"mean          : {mean:+.4f}%")
    print(f"mean | np > 0  : {pos.mean():+.4f}%  (frac>0 = {pos.size / n:.4f})")
    print(f"p1 / p50 / p99 : {np.percentile(np_, 1):+.3f}% / "
          f"{np.percentile(np_, 50):+.3f}% / {np.percentile(np_, 99):+.3f}%")
    print(f"min / max     : {np_.min():+.3f}% / {np_.max():+.3f}%")

    fig, (axL, axR) = plt.subplots(1, 2, figsize=(14, 5))

    # Left: full range, log-y.
    lo, hi = np.percentile(np_, [0.01, 99.99])
    axL.hist(np_, bins=args.bins, range=(lo, hi), color="steelblue")
    axL.set_yscale("log")
    axL.axvline(0, color="k", lw=0.8)
    axL.axvline(mean, color="crimson", lw=1.2, ls="--", label=f"mean {mean:+.3f}%")
    axL.set_title(f"net_pressure — full range, log-y  (n={n:,})")
    axL.set_xlabel("net_pressure [%]")
    axL.set_ylabel("count (log)")
    axL.legend()

    # Right: zoomed to +/-10%, linear-y.
    axR.hist(np_, bins=args.bins, range=(-10, 10), color="seagreen")
    axR.axvline(0, color="k", lw=0.8)
    axR.axvline(mean, color="crimson", lw=1.2, ls="--", label=f"mean {mean:+.3f}%")
    axR.set_title("net_pressure — zoom +/-10%, linear-y")
    axR.set_xlabel("net_pressure [%]")
    axR.set_ylabel("count")
    axR.legend()

    fig.tight_layout()
    fig.savefig(args.out, dpi=120)
    print(f"saved {args.out}")
    if not args.save_only:
        plt.show()


if __name__ == "__main__":
    main()
