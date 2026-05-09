"""Histogram of per-user relative PnL (net_pnl_redem / avg_pos), in matplotlib.

Reads pnl_all_users.csv produced by all_users_pnl.py. The x-axis is the
unitless return: net PnL relative to a user's time-weighted average
position size during their active holding period. NB: this is
*absolute* (not annualized) return over each user's individual window.

Bar height = sum of avg_pos (BTC-equivalent) in the bin. So each user's
contribution to the histogram is weighted by how big their typical
position was — a whale with 100 BTC counts 100,000× more than a user
with 0.001 BTC. Reads as "total BTC-volume that experienced this
relative return."

Usage:
    uv run python scripts/plot_pnl_histogram.py [CSV_PATH] [--low L --high H]
"""
from __future__ import annotations

import os
import sys

import matplotlib

# QtAgg via pyqt6 — single pip dep with bundled Qt binaries, no system
# rebuild needed. The system default GTK4Cairo backend would need both
# pycairo and PyGObject and a working GTK4 stack.
matplotlib.use("QtAgg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from yb import all_markets, w3  # noqa: E402

POOL_ABI = [{"name": "price_oracle", "type": "function", "stateMutability": "view",
             "inputs": [], "outputs": [{"type": "uint256"}]}]


# Asset/BTC ratios used to convert avg_pos to BTC-equivalent. Live values
# are fetched if the RPC is reachable, otherwise this snapshot is used.
# Values from a previous live run with WBTC ≈ $79,692:
FALLBACK_FACTORS: dict[int, float] = {
    3: 1.000,    # WBTC
    4: 1.004,    # cbBTC
    5: 0.999,    # tBTC
    6: 0.0291,   # WETH at ~$2,315 vs $79,692 BTC
}


def asset_to_btc_factors() -> dict[int, float]:
    """Per-market price_oracle, normalized to WBTC. RPC if up, else fallback."""
    try:
        client = w3()
        by_idx = {m.idx: m for m in all_markets()}
        raw = {}
        for idx in (3, 4, 5, 6):
            pool = client.eth.contract(address=by_idx[idx].cryptopool, abi=POOL_ABI)
            raw[idx] = pool.functions.price_oracle().call() / 1e18
        btc_ref = raw[3]
        return {idx: p / btc_ref for idx, p in raw.items()}
    except Exception as e:
        print(f"RPC unavailable ({type(e).__name__}); using FALLBACK_FACTORS")
        return FALLBACK_FACTORS


def main() -> None:
    args = sys.argv[1:]
    low, high, bin_width = -0.25, 0.25, 0.005
    pnl_col = "net_pnl_redem"
    pnl_label = "redemption-view (preview_withdraw)"
    while ("--low" in args or "--high" in args
           or "--pps" in args or "--redeem" in args):
        if "--low" in args:
            i = args.index("--low")
            low = float(args[i + 1])
            args = args[:i] + args[i + 2:]
        if "--high" in args:
            i = args.index("--high")
            high = float(args[i + 1])
            args = args[:i] + args[i + 2:]
        if "--pps" in args:
            args.remove("--pps")
            pnl_col = "net_pnl_pps"
            pnl_label = "NAV-view (pricePerShare)"
        if "--redeem" in args:
            args.remove("--redeem")
            pnl_col = "net_pnl_redem"
            pnl_label = "redemption-view (preview_withdraw)"
    csv_path = args[0] if args else "pnl_all_users.csv"

    factors = asset_to_btc_factors()  # market_idx -> asset/BTC ratio at current oracle
    df = pl.read_csv(csv_path)
    df = df.filter(pl.col("avg_pos") > 0)
    df = df.with_columns(
        rel=pl.col(pnl_col) / pl.col("avg_pos"),
        avg_pos_btc=pl.col("avg_pos") * pl.col("market").replace_strict(factors),
    )
    df = df.filter(pl.col("rel").is_finite())

    inside = df.filter((pl.col("rel") >= low) & (pl.col("rel") <= high))
    clip_lo = (df["rel"] < low).sum()
    clip_hi = (df["rel"] > high).sum()
    vals = inside["rel"].to_numpy()
    weights = inside["avg_pos_btc"].to_numpy()
    # Stats on the weighted distribution.
    sort_ix = np.argsort(vals)
    s_vals = vals[sort_ix]
    s_w = weights[sort_ix]
    cum = np.cumsum(s_w)
    median = float(s_vals[np.searchsorted(cum, cum[-1] / 2)])
    mean = float((vals * weights).sum() / weights.sum())

    print(f"CSV: {csv_path}  (non-trivial users: {len(df)}, "
          f"shown: {len(vals)}, clipped: {clip_lo} below / {clip_hi} above)")
    print(f"  weighted median: {median:+.1%}    weighted mean: {mean:+.1%}")
    print(f"  total avg_pos in shown bins: {weights.sum():.4f} BTC-equiv")

    n_bins = round((high - low) / bin_width)
    fig, ax = plt.subplots(figsize=(13, 6))
    ax.hist(vals, bins=n_bins, range=(low, high), weights=weights,
            edgecolor="black", alpha=0.85, linewidth=0.5)
    ax.axvline(0, color="red", linestyle="--", linewidth=1, label="0%")
    ax.axvline(median, color="green", linestyle=":", linewidth=1.5,
               label=f"weighted median = {median:+.1%}")
    ax.axvline(mean, color="orange", linestyle=":", linewidth=1.5,
               label=f"weighted mean = {mean:+.1%}")
    ax.xaxis.set_major_formatter(mticker.PercentFormatter(xmax=1, decimals=0))
    ax.xaxis.set_major_locator(mticker.MultipleLocator(0.05))
    ax.xaxis.set_minor_locator(mticker.MultipleLocator(0.01))
    ax.set_xlabel(f"Net PnL as % of avg position size — {pnl_label}  (absolute)")
    ax.set_ylabel("Σ avg position size in bin  (BTC-equivalent)")
    ax.set_title(
        f"YB markets 3–6: BTC-volume by per-user PnL ratio — {pnl_label}  "
        f"(n={len(vals)}; clipped {clip_lo}<{low:+.0%}, {clip_hi}>{high:+.0%})"
    )
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()

    out_png = f"pnl_histogram_{'pps' if pnl_col == 'net_pnl_pps' else 'redem'}.png"
    fig.savefig(out_png, dpi=120)
    print(f"Saved {out_png}")

    plt.show()


if __name__ == "__main__":
    main()
