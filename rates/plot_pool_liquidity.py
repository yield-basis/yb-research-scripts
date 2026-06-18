"""Plot total pool liquidity (balances(0)+balances(1)) vs time.

Reads the CSV produced by fetch_pool_liquidity.py and plots liquidity against
the block timestamp. Used to eyeball how quickly liquidity reacts to incentives;
fit regions / time constants are added later once regions are chosen.

Usage
-----
    uv run python plot_pool_liquidity.py
    uv run python plot_pool_liquidity.py --in pool_liquidity.csv.xz --log
"""
from __future__ import annotations

import argparse
import datetime as dt
import lzma
from pathlib import Path

import matplotlib
# Prefer Qt (pyqt6 is installed) for interactive show; Agg is forced when saving.
if "--save" in __import__("sys").argv:
    matplotlib.use("Agg")
else:
    try:
        matplotlib.use("QtAgg")
    except Exception:
        pass
import matplotlib.pyplot as plt
import polars as pl

HERE = Path(__file__).resolve().parent
DEFAULT_IN = HERE / "pool_liquidity.csv.xz"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", type=Path, default=DEFAULT_IN)
    ap.add_argument("--log", action="store_true", help="log scale on liquidity axis")
    ap.add_argument("--save", type=Path, default=None, help="save PNG instead of showing")
    args = ap.parse_args()

    if str(args.inp).endswith(".xz"):
        with lzma.open(args.inp) as f:
            df = pl.read_csv(f.read())
    else:
        df = pl.read_csv(args.inp)
    times = [dt.datetime.fromtimestamp(t, dt.UTC) for t in df["timestamp"]]
    liq = df["liquidity"].to_numpy()

    fig, ax = plt.subplots(figsize=(13, 6))
    ax.plot(times, liq / 1e6, lw=1.0, color="C0")
    ax.set_xlabel("date (UTC)")
    ax.set_ylabel("pool liquidity, $M  (balances0 + balances1)")
    ax.set_title("PYUSD/crvUSD pool liquidity vs time")
    if args.log:
        ax.set_yscale("log")
    ax.grid(True, alpha=0.3)
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
