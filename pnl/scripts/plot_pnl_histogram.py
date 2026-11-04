"""Distribution of per-user PnL over the window (pnl_all_users.csv).

Reads pnl_all_users.csv from all_users_pnl.py. Net PnL counts unstaked and
gauge-staked LT plus the YB earned in the window, valued in the asset. By
default each user is one entry, summed over the markets they used, in
BTC-equivalent (asset amounts converted at today's pool prices).

Two panels, same x-axis: relative PnL = net PnL / time-weighted average
position (absolute, not annualised, over each user's own holding period).
    top     bar height = sum of average positions in the bin (BTC-eq), i.e. how
            much capital saw that return; a whale counts by size.
    bottom  bar height = number of users, counting only users with an average
            position >= --min-pos BTC-eq (default 0.001): most small accounts
            hold dust plus a little YB and would swamp the count.
Edge bins hold the clipped tails (--low / --high, default -10% / +10%).

PnL view (default `net_pnl`: redemption for v1, pricePerShare for v2/v3, see
all_users_pnl.py):
    --pps / --redeem   one pure view for all markets instead
    --by-market        one entry per (market, user) instead of per user
    --no-show          only save the PNG (no interactive window)

Outputs: pnl_histogram_{net|pps|redem}[_by_market].png, and (per-user mode)
pnl_per_user.csv with per-user totals in BTC-eq for the chosen view.

Usage:
    uv run python scripts/plot_pnl_histogram.py [CSV_PATH] [--low L --high H] [--min-pos P]
                                                [--pps | --redeem] [--by-market] [--no-show]
"""
from __future__ import annotations

import os
import sys

import matplotlib

# Interactive windows need QtAgg (pyqt6 bundles Qt); the system default
# GTK4Cairo backend would need pycairo + PyGObject. Saving only: Agg.
matplotlib.use("Agg" if "--no-show" in sys.argv else "QtAgg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from yb import WINDOW_START, all_markets, w3  # noqa: E402

POOL_ABI = [{"name": "price_oracle", "type": "function", "stateMutability": "view",
             "inputs": [], "outputs": [{"type": "uint256"}]}]

# Same validated palette as btc-fundamental-pnl: diverging profit / loss poles,
# neutral inks and chrome.
POS, NEG = "#2a78d6", "#e34948"
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "axes.labelcolor": INK2,
    "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlecolor": INK,
    "axes.titlelocation": "left", "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "axes.axisbelow": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "xtick.color": MUTED, "ytick.color": MUTED, "font.size": 9, "legend.frameon": False,
})

PCT = mticker.FuncFormatter(lambda x, _: f"{x:+.0%}" if abs(x) > 1e-12 else "0%")

VIEWS = {"net_pnl": ("net", "v1 redemption, v2/v3 pricePerShare"),
         "net_pnl_pps": ("pps", "pricePerShare (NAV) for all markets"),
         "net_pnl_redem": ("redem", "redemption (preview_withdraw) for all markets")}


def asset_to_btc_factors(df: pl.DataFrame) -> dict[int, float]:
    """Asset price / BTC price per market, from the pools' price_oracle (WBTC = 1).

    Falls back to 1 for BTC-family assets and a rough ETH/BTC for WETH if the
    RPC is unreachable.
    """
    symbols = dict(df.select("market", "symbol").unique().iter_rows())
    try:
        client = w3()
        by_idx = {m.idx: m for m in all_markets()}

        def price(idx):
            pool = client.eth.contract(address=by_idx[idx].cryptopool, abi=POOL_ABI)
            return pool.functions.price_oracle().call() / 1e18

        btc_ref = price(min(i for i, s in symbols.items() if s == "WBTC"))
        return {idx: price(idx) / btc_ref for idx in symbols}
    except Exception as e:
        print(f"RPC unavailable ({type(e).__name__}); using rough asset/BTC factors")
        return {idx: (1.0 if "BTC" in s else 0.03) for idx, s in symbols.items()}


def weighted_quantile(vals, weights, q):
    ix = np.argsort(vals)
    cum = np.cumsum(weights[ix])
    return float(vals[ix][np.searchsorted(cum, q * cum[-1])])


def main() -> None:
    args = sys.argv[1:]
    low, high, bin_width = -0.10, 0.10, 0.0025
    min_pos = 0.001  # BTC-eq average position for the per-user count panel
    pnl_col = "net_pnl"
    by_market, show = "--by-market" in args, "--no-show" not in args
    if "--min-pos" in args:
        i = args.index("--min-pos")
        min_pos = float(args[i + 1])
        args = args[:i] + args[i + 2:]
    for flag in ("--by-market", "--no-show"):
        if flag in args:
            args.remove(flag)
    while "--low" in args or "--high" in args or "--pps" in args or "--redeem" in args:
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
        if "--redeem" in args:
            args.remove("--redeem")
            pnl_col = "net_pnl_redem"
    csv_path = args[0] if args else "pnl_all_users.csv"
    tag, view_label = VIEWS[pnl_col]

    df = pl.read_csv(csv_path)
    if pnl_col not in df.columns:
        raise SystemExit(f"{csv_path} has no {pnl_col} column; rerun all_users_pnl.py")
    factors = asset_to_btc_factors(df)
    f = pl.col("market").replace_strict(factors)
    df = df.with_columns(
        pnl_btc=pl.col(pnl_col) * f,
        yb_btc=pl.col("yb_value_in_asset") * f,
        avg_pos_btc=pl.col("avg_pos") * f,
        max_pos_btc=pl.col("max_pos") * f,
    )
    if by_market:
        per = df.select("user", "market", "version", "avg_pos_btc", "max_pos_btc",
                        "pnl_btc", "yb_btc")
    else:
        per = (df.group_by("user")
                 .agg(pl.col("market").sort().cast(pl.String).str.join(" ").alias("markets"),
                      pl.col("version").unique().sort().str.join(" ").alias("versions"),
                      pl.col("avg_pos_btc").sum(), pl.col("max_pos_btc").sum(),
                      pl.col("pnl_btc").sum(), pl.col("yb_btc").sum()))
    per = per.with_columns(
        pnl_ex_yb_btc=pl.col("pnl_btc") - pl.col("yb_btc"),
        rel=pl.col("pnl_btc") / pl.col("avg_pos_btc"),
    ).sort("pnl_btc")
    if not by_market:
        per.write_csv("pnl_per_user.csv")

    unit = "(market, user) entries" if by_market else "users"
    held = per.filter((pl.col("avg_pos_btc") > 0) & pl.col("rel").is_finite())
    vals, weights = held["rel"].to_numpy(), held["avg_pos_btc"].to_numpy()
    median = weighted_quantile(vals, weights, 0.5)
    mean = float((vals * weights).sum() / weights.sum())
    pnl = per["pnl_btc"].to_numpy()
    win = pnl > 0

    markets = sorted(df["market"].unique().to_list())
    print(f"CSV: {csv_path}   view: {view_label}   markets {markets}")
    print(f"  {len(per):,} {unit} with activity since {WINDOW_START} UTC "
          f"({len(held):,} held a position)")
    print(f"  in profit: {win.sum():,} ({win.mean():.1%} of {unit}); "
          f"capital-weighted: {weights[vals > 0].sum() / weights.sum():.1%}")
    print(f"  total net PnL {pnl.sum():+.3f} BTC-eq = positions {per['pnl_ex_yb_btc'].sum():+.3f} "
          f"+ YB {per['yb_btc'].sum():+.3f}")
    print(f"  relative PnL, weighted by average position: median {median:+.2%}, "
          f"mean {mean:+.2%}, 10-90% [{weighted_quantile(vals, weights, 0.1):+.2%}, "
          f"{weighted_quantile(vals, weights, 0.9):+.2%}]")
    # Users with a non-trivial position (most small accounts only hold dust + some YB)
    real = held.filter(pl.col("avg_pos_btc") >= min_pos)
    real_pnl = real["pnl_btc"].to_numpy()
    real_win = float((real_pnl > 0).mean())
    print(f"  {len(real):,} {unit} with average position >= {min_pos:g} BTC-eq: "
          f"{real_win:.1%} in profit; median return {real['rel'].median():+.2%}")
    q = np.quantile(real_pnl, [0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99])
    print("    their net PnL (BTC-eq) quantiles 1/10/25/50/75/90/99%: "
          + "  ".join(f"{x:+.4f}" for x in q))
    cols = ["user", "markets" if not by_market else "market", "avg_pos_btc", "pnl_btc",
            "yb_btc", "rel"]
    with pl.Config(tbl_rows=12, tbl_cols=10, tbl_width_chars=200, float_precision=4):
        print("  biggest losses:")
        print(per.head(6).select(cols))
        print("  biggest gains:")
        print(per.tail(6).reverse().select(cols))

    fig, (ax_rel, ax_abs) = plt.subplots(2, 1, figsize=(12, 9),
                                         gridspec_kw={"height_ratios": [1.3, 1]})

    # ---- top: relative PnL, capital-weighted
    clip_lo, clip_hi = int((vals < low).sum()), int((vals > high).sum())
    edges = np.linspace(low, high, round((high - low) / bin_width) + 1)
    h, _ = np.histogram(np.clip(vals, low, high), bins=edges, weights=weights)
    centers = (edges[:-1] + edges[1:]) / 2
    ax_rel.bar(centers, h, width=bin_width * 0.85, color=np.where(centers >= 0, POS, NEG),
               linewidth=0)
    ax_rel.axvline(0, color=AXIS, lw=0.8)
    ax_rel.axvline(median, color=INK, lw=1.2, ls=(0, (4, 2.5)),
                   label=f"capital-weighted median {median:+.1%}")
    ax_rel.axvline(mean, color=INK2, lw=1.2, ls=(0, (1, 1.5)),
                   label=f"capital-weighted mean {mean:+.1%}")
    ax_rel.xaxis.set_major_formatter(PCT)
    ax_rel.xaxis.set_major_locator(mticker.MultipleLocator(0.02 if high - low <= 0.2 else 0.05))
    ax_rel.xaxis.set_minor_locator(mticker.MultipleLocator(0.005 if high - low <= 0.2 else 0.01))
    ax_rel.set_xlim(low, high)
    ax_rel.set_xlabel("net PnL as % of the user's average position (incl. YB; edge bins "
                      f"hold the clipped tails: {clip_lo} below, {clip_hi} above)")
    ax_rel.set_ylabel("Σ average position in bin (BTC-eq)")
    ax_rel.set_title(f"Per-user PnL since {WINDOW_START} UTC, staked + unstaked + YB — "
                     f"{len(held):,} {unit}, markets {markets[0]}–{markets[-1]}")
    ax_rel.legend(loc="upper left")

    # ---- bottom: the same return, one count per user, dust excluded
    n, _ = np.histogram(np.clip(real["rel"].to_numpy(), low, high), bins=edges)
    ax_abs.bar(centers, n, width=bin_width * 0.85, color=np.where(centers >= 0, POS, NEG),
               linewidth=0)
    ax_abs.axvline(0, color=AXIS, lw=0.8)
    ax_abs.xaxis.set_major_formatter(PCT)
    ax_abs.xaxis.set_major_locator(mticker.MultipleLocator(0.02 if high - low <= 0.2 else 0.05))
    ax_abs.xaxis.set_minor_locator(mticker.MultipleLocator(0.005 if high - low <= 0.2 else 0.01))
    ax_abs.set_xlim(low, high)
    ax_abs.set_xlabel("net PnL as % of the user's average position (incl. YB)")
    ax_abs.set_ylabel(f"number of {unit}")
    ax_abs.set_title(f"Same, one count per user: {len(real):,} {unit} with an average position "
                     f"≥ {min_pos:g} BTC-eq, {real_win:.0%} in profit "
                     f"({len(held) - len(real):,} smaller ones left out)")
    fig.text(0.01, 0.005, f"PnL view: {view_label}. Assets converted to BTC at today's "
             f"pool prices. Total over all {unit}: {pnl.sum():+.2f} BTC-eq = positions "
             f"{per['pnl_ex_yb_btc'].sum():+.2f} + YB {per['yb_btc'].sum():+.2f}.",
             color=MUTED, fontsize=8)
    fig.tight_layout(rect=(0, 0.02, 1, 1))

    out_png = f"pnl_histogram_{tag}{'_by_market' if by_market else ''}.png"
    fig.savefig(out_png, dpi=150)
    print(f"Saved {out_png}" + ("" if by_market else " and pnl_per_user.csv"))
    if show:
        plt.show()


if __name__ == "__main__":
    main()
