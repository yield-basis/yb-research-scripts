#!/usr/bin/env python3
"""Net pressure of the system on crvUSD, from simulated pool dumps.

net_pressure = (levamm_debt - balances[0]) / (balances[0] + balances[1] * price)

balances[0] is the crvUSD side of the pool (token0), balances[1] the BTC side
(token1). LevAMM debt is not stored in the dumps, so we use the (very close)
identity debt ~= (token0 + token1 * price_scale) / 2.

The mean net_pressure should sit near zero; we also report the mean restricted
to points where net_pressure > 0 (the "long crvUSD demand" tail).
"""
import argparse
import glob
import io
import lzma
import os

import numpy as np

# Stored array used as `price` in the denominator (the pool TVL term): the real
# CEX price.
PRICE_KEY = "p_cex"


def load_npz_xz(path):
    with lzma.open(path) as fh:
        return np.load(io.BytesIO(fh.read()), allow_pickle=True)


def net_pressure(d):
    token0 = d["token0"].astype(np.float64)   # crvUSD = balances[0]
    token1 = d["token1"].astype(np.float64)   # BTC    = balances[1]
    price_scale = d["price_scale"].astype(np.float64)
    price = d[PRICE_KEY].astype(np.float64)

    # LevAMM debt is not dumped; this identity is very close to it.
    levamm_debt = (token0 + token1 * price_scale) / 2.0

    tvl = token0 + token1 * price                # denominator
    # Factor of 2: compares the imbalance against the half-deposit (one side),
    # i.e. against tvl/2, not full tvl.
    with np.errstate(divide="ignore", invalid="ignore"):
        np_ = 2.0 * (levamm_debt - token0) / tvl

    valid = np.isfinite(np_) & (tvl > 0)
    return np_[valid]


def summarize(name, np_):
    n = np_.size
    pos = np_[np_ > 0]
    neg = np_[np_ < 0]
    # time weighting would need per-point dt; points are per-trade/per-candle so
    # we report the plain point-wise mean, which is what avg(net_pressure) means.
    print(f"\n=== {name} ===")
    print(f"  points              : {n:,}")
    print(f"  mean(net_pressure)  : {np_.mean() * 100:+.4f}%")
    print(f"  median              : {np.median(np_) * 100:+.4f}%")
    print(f"  mean | np > 0       : {pos.mean() * 100:+.4f}%   (frac>0 = {pos.size / n:.4f})")
    print(f"  mean | np < 0       : {neg.mean() * 100:+.4f}%   (frac<0 = {neg.size / n:.4f})")
    print(f"  min / max           : {np_.min() * 100:+.4f}% / {np_.max() * 100:+.4f}%")
    return {
        "name": name,
        "n": n,
        "mean": float(np_.mean()),
        "mean_pos": float(pos.mean()) if pos.size else float("nan"),
        "frac_pos": pos.size / n,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*",
                    default=sorted(glob.glob("btc-candidates-yb-opt/**/detailed-output.npz.xz",
                                             recursive=True)),
                    help="detailed-output.npz.xz files (default: all candidates)")
    args = ap.parse_args()

    if not args.paths:
        ap.error("no detailed-output.npz.xz files found")

    print(f"price (denominator) = {PRICE_KEY}")

    rows = []
    for path in args.paths:
        d = load_npz_xz(path)
        np_ = net_pressure(d)
        name = os.path.relpath(os.path.dirname(path), "btc-candidates-yb-opt")
        rows.append(summarize(name, np_))

    if len(rows) > 1:
        # Pooled over all points across candidates.
        print("\n=== pooled (all candidates) ===")
        total_n = sum(r["n"] for r in rows)
        w_mean = sum(r["mean"] * r["n"] for r in rows) / total_n
        print(f"  point-weighted mean(net_pressure): {w_mean * 100:+.4f}%  over {total_n:,} points")


if __name__ == "__main__":
    main()
