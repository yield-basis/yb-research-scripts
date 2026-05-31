"""Plot model vs Chainlink vs price_oracle for the crvUSD/cbBTC pool.

Same precision-band model as plot_model_vs_price.py, but with the pool's
on-chain price_oracle() in place of the Binance EMA, and the 1% pool fee as the
band threshold. Per timestamp, with f = POOL_FEE:

    chainlink > (1+f)*price_oracle  ->  chainlink / (1+f)
    chainlink < (1-f)*price_oracle  ->  chainlink / (1-f)
    otherwise                       ->  price_oracle

Inputs (over the pool's lifespan, same window):
  * pool_oracle_scale.csv(.xz)  — per-block price_oracle/price_scale + time
                                   (from fetch_pool_oracle.py)
  * chainlink_pool_window.csv(.xz) — Chainlink BTC/USD rounds, forward-filled
                                   onto the pool block grid (from fetch_chainlink.py)

Three lines, all viewport-decimated / lazily redrawn on zoom:
  price_oracle (red), Chainlink (gray), model (black).

Usage
-----
    uv run python plot_pool_model.py
    uv run python plot_pool_model.py --fee 0.01 --save pool_model.png
"""
from __future__ import annotations

import argparse
import csv
import lzma
from pathlib import Path

import numpy as np

from plot_chainlink_vs_price import CACHE, LazyPlot, load_oracle

HERE = Path(__file__).resolve().parent
DEFAULT_POOL = HERE / "pool_oracle_scale.csv.xz"
DEFAULT_CHAINLINK = HERE / "chainlink_pool_window.csv.xz"

# Pool fee = precision band for the model (1%). Configurable here / via --fee.
POOL_FEE = 0.01


def model_price(price_oracle: np.ndarray, chainlink: np.ndarray,
                fee: float) -> np.ndarray:
    """Clamp Chainlink to the edge of the +/-fee band around price_oracle, and
    fall back to price_oracle inside the band. Vectorized."""
    po = np.asarray(price_oracle, dtype=np.float64)
    cl = np.asarray(chainlink, dtype=np.float64)
    return np.select(
        [cl > (1 + fee) * po, cl < (1 - fee) * po],
        [cl / (1 + fee), cl / (1 - fee)],
        default=po,
    )


def load_pool(path: Path):
    """Return (timestamp, price_oracle, price_scale) float64 arrays. Cached."""
    CACHE.mkdir(exist_ok=True)
    cache_f = CACHE / f"pool_{path.stem}_{int(path.stat().st_mtime)}.npz"
    if cache_f.exists():
        d = np.load(cache_f)
        return d["ts"], d["po"], d["ps"]
    op = lzma.open(path, "rt") if path.suffix == ".xz" else open(path)
    ts, po, ps = [], [], []
    with op as fh:
        for row in csv.DictReader(fh):
            ts.append(float(row["timestamp"]))
            po.append(float(row["price_oracle"]))
            ps.append(float(row["price_scale"]))
    ts = np.asarray(ts, dtype=np.float64)
    po = np.asarray(po, dtype=np.float64)
    ps = np.asarray(ps, dtype=np.float64)
    np.savez(cache_f, ts=ts, po=po, ps=ps)
    return ts, po, ps


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", type=Path, default=DEFAULT_POOL)
    ap.add_argument("--chainlink", type=Path, default=DEFAULT_CHAINLINK)
    ap.add_argument("--fee", type=float, default=POOL_FEE,
                    help=f"precision band / pool fee (default {POOL_FEE})")
    ap.add_argument("--save", type=Path, default=None)
    ap.add_argument("--width", type=int, default=2400)
    args = ap.parse_args()

    import matplotlib
    if args.save is not None:
        matplotlib.use("Agg")
    else:
        matplotlib.use("QtAgg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    epoch = mdates.date2num(np.datetime64("1970-01-01T00:00:00"))

    print(f"loading pool      {args.pool.name} …", flush=True)
    ts_p, po, _ps = load_pool(args.pool)
    print(f"loading chainlink {args.chainlink.name} …", flush=True)
    ts_o, px_o = load_oracle(args.chainlink)
    print(f"pool samples: {ts_p.size:,}   chainlink rounds: {ts_o.size:,}",
          flush=True)

    # Forward-fill Chainlink onto the pool block grid (as-of join on time).
    idx = np.searchsorted(ts_o, ts_p, side="right") - 1
    n_lead = int((idx < 0).sum())
    idx = np.clip(idx, 0, ts_o.size - 1)
    cl_grid = px_o[idx]
    if n_lead:
        print(f"note: {n_lead} leading pool sample(s) before first Chainlink "
              f"round clamped to first price {px_o[0]:,.2f}", flush=True)

    model = model_price(po, cl_grid, args.fee)
    above = float((cl_grid > (1 + args.fee) * po).mean())
    below = float((cl_grid < (1 - args.fee) * po).mean())
    print(f"model branches: above={above*100:.2f}%  below={below*100:.2f}%  "
          f"in-band(=price_oracle)={ (1-above-below)*100:.2f}%", flush=True)

    xc = epoch + ts_p / 86400.0

    fig, ax = plt.subplots(figsize=(15, 7))
    ax.set_autoscaley_on(False)
    (line_oracle,) = ax.plot([], [], lw=1.2, color="red",
                             label="pool price_oracle()")
    (line_cl,) = ax.plot([], [], lw=1.2, color="gray", alpha=0.8,
                         label="Chainlink price (on grid)")
    (line_model,) = ax.plot([], [], lw=1.2, color="black", zorder=4,
                            label="model price")

    ax.set_xlim(xc[0], xc[-1])
    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.set_ylabel("BTC price (USD)")
    ax.set_title(f"crvUSD/cbBTC pool: model vs Chainlink vs price_oracle "
                 f"(fee={args.fee*100:g}%)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")

    # price_oracle is the primary dense series; Chainlink + model ride along.
    lazy = LazyPlot(ax, xc, po, line_oracle, xo=None,
                    extra=[(cl_grid, line_cl), (model, line_model)])
    ax.callbacks.connect("xlim_changed", lazy.redraw)
    fig.canvas.mpl_connect("resize_event", lazy.redraw)

    fig.tight_layout()
    lazy.redraw()

    if args.save is not None:
        fig.canvas.draw()
        lazy.redraw()
        fig.savefig(args.save, dpi=args.width / 15)
        print(f"wrote {args.save}")
    else:
        print("interactive: zoom/pan re-decimates all series to the view.",
              flush=True)
        plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
