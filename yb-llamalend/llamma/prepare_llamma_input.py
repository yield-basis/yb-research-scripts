"""trajectory.csv -> the llamma_scan input: int64 n, int64 t[n], float64 oracle[n], float64 market[n].

oracle: the price the LLAMMA reads, weth.oracles-ll.yieldbasis.eth (oracle_ll_weth, checkpointed every row: the
keeper); market: where arbitrageurs trade the LT, its redemption value (redemption_weth). Both in WETH per LT.

usage: python prepare_llamma_input.py runs/<name>/trajectory.csv OUT.bin [--oracle oracle_ll_weth]
"""

import argparse
import csv

import numpy as np

parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
parser.add_argument("csv")
parser.add_argument("out")
parser.add_argument("--oracle", default="oracle_ll_weth")
parser.add_argument("--market", default="redemption_weth")
args = parser.parse_args()

with open(args.csv, newline="") as stream:
    rows = list(csv.DictReader(stream))
t = np.array([int(r["timestamp"]) for r in rows], dtype=np.int64)
oracle = np.array([float(r[args.oracle]) for r in rows])
market = np.array([float(r[args.market]) for r in rows])
assert np.all(np.diff(t) > 0) and np.all(oracle > 0) and np.all(market > 0)
with open(args.out, "wb") as f:
    np.array([len(t)], dtype=np.int64).tofile(f)
    t.tofile(f)
    oracle.tofile(f)
    market.tofile(f)
print(f"wrote {args.out}: {len(t)} rows, {t[0]} .. {t[-1]}, median step {np.median(np.diff(t)):.0f} s")
