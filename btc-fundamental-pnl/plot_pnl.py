"""Plots of the fundamental (users + admin) PnL of the YB BTC markets.

Reads data/pnl.csv.xz (compute_pnl.py), which starts at the window start
(--start there), and writes, as both PDF and PNG:
    pics/btc_relative_pnl        relative PnL of all BTC markets: system and users' part
                                 (after admin fee), each as designed + reliable
    pics/<asset>_relative_pnl    the same per asset across versions: wbtc, cbbtc, tbtc, weth
    pics/btc_fundamental_pnl     relative PnL of all BTC markets (system = users + admin
                                 fee charged) with the "reliable" dashed line, the
                                 non-inflated (balanced-out) value, the same in BTC,
                                 BTC price, weekly APR, vol
    pics/btc_pnl_by_market       per-asset small multiples: relative PnL per version + equity
    pics/btc_pnl_valuations      relative PnL under every valuation, latent cost of
                                 balancing out, and how far each pool's price_scale
                                 lagged the market
    pics/btc_apr_vs_vol          weekly APR against weekly realised volatility, split by
                                 whether the pools' price_scale kept up with the market

Usage:
    uv run python plot_pnl.py [--show] [--dev-threshold 0.02]
    (--dev-threshold only drives the shading; use the one compute_pnl.py ran with)
"""
from __future__ import annotations

import argparse
import json
import sys

import matplotlib

matplotlib.use("QtAgg" if "--show" in sys.argv else "Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from yb_common import DATA, PICS, SECONDS_PER_YEAR, is_btc, realised_vol  # noqa: E402

# Reference categorical slots 1-3 (validated all-pairs), ink and chrome
VERSION_COLOR = {"v1": "#2a78d6", "v2": "#eb6834", "v3": "#1baf7a"}
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
POS, NEG = "#2a78d6", "#e34948"  # diverging poles: profit / loss
USERS, ADMIN_FILL = "#2a78d6", "#b7d3f6"  # users' part; admin fee = gap to the system line
LAG_THRESHOLD = 0.10             # equity-weighted |price_scale / oracle - 1| for "lagging"
MKT = "#4a3aa7"                  # mark-to-market valuation (slot 7, distinct from versions)
BAL, BAL_LIGHT = "#4a3aa7", "#9085e9"   # balanced out (gradual) / its latent-cost fill
SHADE = "#f0efec"                # neutral: periods when the measurement is unreliable


def tint(color, alpha=0.14, base=SURFACE):
    """Pale version of `color`: alpha-blend over the chart surface."""
    c, b = (np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)]) for h in (color, base))
    return "#" + "".join(f"{int(round(v)):02x}" for v in alpha * c + (1 - alpha) * b)


VERSION_TINT = {v: tint(c) for v, c in VERSION_COLOR.items()}

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS, "axes.linewidth": 0.8, "axes.labelcolor": INK2,
    "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlecolor": INK,
    "axes.titlelocation": "left", "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "grid.linestyle": "-",
    "xtick.color": MUTED, "ytick.color": MUTED, "font.size": 9,
    "legend.frameon": False, "lines.linewidth": 1.4,
})


def load():
    markets = {m["idx"]: m for m in json.loads((DATA / "markets.json").read_text())}
    w = pd.read_csv(DATA / "pnl.csv.xz")
    w.index = pd.to_datetime(w["timestamp"], unit="s", utc=True).rename("dt")
    return markets, w


def launches(markets):
    """First block-time of each version (market creation), from the wide table."""
    return {v: min(m["created_block"] for m in markets.values() if m["version"] == v)
            for v in ("v1", "v2", "v3")}


def mark_eras(ax, w, markets, label=False):
    for v, block in launches(markets).items():
        if v == "v1":
            continue
        t = w.index[w["block"].searchsorted(block)]
        ax.axvline(t, color=AXIS, lw=0.8, zorder=0)
        if label:
            ax.annotate(f"{v} launch", (t, 1), xycoords=("data", "axes fraction"),
                        xytext=(3, -2), textcoords="offset points", va="top",
                        color=MUTED, fontsize=8)


def weekly(w: pd.DataFrame, markets) -> pd.DataFrame:
    """Weekly system return (users + admin) as APR, admin share, equity, realised vol."""
    dt_s = w["timestamp"].diff().shift(-1).fillna(0)
    wk = dict(rule="W-MON", label="left", closed="left")
    eq_time = (w["e_adj_total"] * dt_s).resample(**wk).sum()
    secs = dt_s.resample(**wk).sum()
    ret = (1 + w["r_adj_total"]).resample(**wk).prod() - 1
    ret_users = (1 + w["r_adj_total"] - w["r_admin_total"]).resample(**wk).prod() - 1
    cum = w["pnl_adj_total"].resample(**wk).last()
    vol = w["btc_usd"].resample(**wk).apply(realised_vol)
    out = pd.DataFrame({"ret": ret, "ret_users": ret_users, "pnl_btc": cum.diff().fillna(cum),
                        "equity": eq_time / secs, "secs": secs, "vol": vol})
    out["apr"] = out["ret"] * SECONDS_PER_YEAR / out["secs"]
    # dominant version by equity, for colouring
    eq_v = pd.DataFrame({v: (w[f"e_adj_{v}"] * dt_s).resample(**wk).sum()
                         for v in ("v1", "v2", "v3")})
    out["version"] = eq_v.idxmax(axis=1)
    # equity-weighted distance of the pools' price_scale from the market
    lag = sum((w[f"ps_dev_{i}"].abs().fillna(0) * w[f"e_adj_{i}"] * dt_s) for i in markets)
    out["ps_lag"] = lag.resample(**wk).sum() / eq_time.replace(0, np.nan)
    # drop partial weeks and near-empty starts
    return out[(out["secs"] > 5 * 86400) & (out["equity"] > 1.0)]


def save(fig, name):
    PICS.mkdir(exist_ok=True)
    for ext in ("pdf", "png"):
        fig.savefig(PICS / f"{name}.{ext}", dpi=200, bbox_inches="tight")
    print(f"-> {PICS / name}.pdf/.png")


def pct(idx: pd.Series) -> pd.Series:
    return (idx - 1) * 100


def since(w: pd.DataFrame) -> str:
    return f"since {w.index[0]:%-d %b %Y %H:%M} UTC"


def label_end(ax, w, y, text, color, **kw):
    ax.annotate(text, (w.index[-1], y), xytext=(4, 0), textcoords="offset points",
                va="center", color=color, fontsize=8.5, **kw)


def label_ends(ax, w, items, min_gap_pt=14):
    """End-of-line labels [(y, text, color, kw)], nudged apart so they don't overlap."""
    ax.get_ylim()  # apply any pending autoscale, so transData maps y correctly
    to_pt = ax.transData.transform
    items = sorted(items, key=lambda it: it[0])
    placed = []
    for y, text, color, kw in items:
        y_pt = to_pt((0, y))[1]
        if placed and y_pt - placed[-1] < min_gap_pt:
            y_pt = placed[-1] + min_gap_pt
        placed.append(y_pt)
        ax.annotate(text, (w.index[-1], y), xytext=(4, y_pt - to_pt((0, y))[1]),
                    textcoords="offset points", va="center", color=color, fontsize=8.5, **kw)


def unreliable_spans(markets, w, threshold):
    """Time spans where most equity sits in pools with |price_scale/oracle - 1| > threshold."""
    eq = sum(w[f"e_adj_{i}"] for i in markets)
    off = sum(w[f"e_adj_{i}"] * (w[f"ps_dev_{i}"].abs() > threshold) for i in markets)
    flag = (off > 0.5 * eq).to_numpy()
    edges = np.flatnonzero(np.diff(np.r_[0, flag.astype(int), 0]))
    return [(w.index[a], w.index[min(b, len(w) - 1)]) for a, b in zip(edges[::2], edges[1::2])]


def shade(ax, spans, label=None):
    for i, (a, b) in enumerate(spans):
        ax.axvspan(a, b, color=SHADE, lw=0, zorder=0, label=label if i == 0 else None)


def fig_main(markets, w, wk, threshold):
    fig, axes = plt.subplots(6, 1, figsize=(11, 18), sharex=True,
                             gridspec_kw={"height_ratios": [3, 2.2, 1.4, 1.3, 1.5, 1.1]})
    ax_rel, ax_bal, ax_btc, ax_px, ax_apr, ax_vol = axes
    spans = unreliable_spans(markets, w, threshold)

    system, users = pct(w["idx_adj_total"]), pct(w["idx_users_total"])
    ax_rel.fill_between(w.index, users, system, color=ADMIN_FILL, lw=0,
                        label="admin fee charged (gap)")
    ax_rel.plot(w.index, users, color=USERS, lw=1.3, label="users' part (after admin fee)")
    ax_rel.plot(w.index, system, color=INK, lw=2.0,
                label="system = users + admin fee, pools as designed")
    reliable = pct(w["idx_rel_total"])
    users_rel = pct(w["idx_users_rel_total"])
    ax_rel.plot(w.index, reliable, color=INK, lw=1.4, ls=(0, (4, 2.5)),
                label=f"reliable: while price_scale is >{threshold:.0%} off, held at max(start, non-inflated value)")
    ax_rel.plot(w.index, users_rel, color=USERS, lw=1.2, ls=(0, (4, 2.5)),
                label="users' part, reliable")
    shade(ax_rel, spans, label=f"most equity in pools >{threshold:.0%} off the market")
    top = max(system.max(), reliable.max())
    bottom = min(users.min(), users_rel.min())
    ax_rel.set_ylim(top=top + 0.9 * (top - bottom))  # headroom for the legend
    label_ends(ax_rel, w, [
        (reliable.iloc[-1], f"{reliable.iloc[-1]:+.2f}%", INK, {}),
        (system.iloc[-1], f"{system.iloc[-1]:+.2f}%", INK, {"fontweight": "bold"}),
        (users.iloc[-1], f"{users.iloc[-1]:+.2f}%", USERS, {}),
        (users_rel.iloc[-1], f"{users_rel.iloc[-1]:+.2f}%", USERS, {})])
    ax_rel.axhline(0, color=AXIS, lw=0.8)
    ax_rel.set_title("Relative fundamental PnL of all YB BTC markets "
                     f"(v1 → v2 → v3, equity-weighted), {since(w)}")
    ax_rel.set_ylabel("cumulative return, % of BTC equity")
    ax_rel.legend(loc="upper left", bbox_to_anchor=(0, 0.95), fontsize=8, ncol=2)
    mark_eras(ax_rel, w, markets, label=True)

    bal = pct(w["idx_bal_total"])
    ax_bal.plot(w.index, system, color=INK, lw=1.2, label="as designed (above)")
    ax_bal.plot(w.index, bal, color=BAL, lw=1.6,
                label="balanced out at the market price, fees returned (non-inflated)")
    label_end(ax_bal, w, bal.iloc[-1], f"{bal.iloc[-1]:+.2f}%", BAL, fontweight="bold")
    label_end(ax_bal, w, system.iloc[-1], f"{system.iloc[-1]:+.2f}%", INK)
    shade(ax_bal, spans)
    ax_bal.axhline(0, color=AXIS, lw=0.8)
    ax_bal.set_title("Non-inflated: what the system would be worth if balanced out at each moment")
    ax_bal.set_ylabel("cumulative return, %")
    ax_bal.legend(loc="lower left", fontsize=8)
    mark_eras(ax_bal, w, markets)

    ax_btc.plot(w.index, w["admin_total"], color=MUTED, lw=1.3, label="of which admin fee charged")
    ax_btc.plot(w.index, w["pnl_users_total"], color=USERS, lw=1.3,
                label="users' part (system − admin fee)")
    ax_btc.plot(w.index, w["pnl_users_rel_total"], color=USERS, lw=1.1, ls=(0, (4, 2.5)),
                label="users' part, reliable")
    ax_btc.plot(w.index, w["pnl_adj_total"], color=INK, lw=1.6, label="system PnL")
    label_ends(ax_btc, w, [
        (w[c].iloc[-1], f"{w[c].iloc[-1]:+.1f}", color, kw)
        for c, color, kw in (("pnl_adj_total", INK, {"fontweight": "bold"}),
                             ("pnl_users_total", USERS, {}), ("pnl_users_rel_total", USERS, {}),
                             ("admin_total", MUTED, {}))])
    ax_btc.axhline(0, color=AXIS, lw=0.8)
    ax_btc.set_title("The same in absolute terms")
    ax_btc.set_ylabel("BTC")
    ax_btc.legend(loc="upper left")
    mark_eras(ax_btc, w, markets)

    ax_px.plot(w.index, w["btc_usd"] / 1e3, color=INK2, lw=1.2)
    ax_px.set_title("BTC price (Chainlink BTC/USD)")
    ax_px.set_ylabel("k USD")
    mark_eras(ax_px, w, markets)

    colors = np.where(wk["apr"] >= 0, POS, NEG)
    ax_apr.bar(wk.index + pd.Timedelta(days=3.5), wk["apr"] * 100, width=5.6,
               color=colors, linewidth=0)
    ax_apr.axhline(0, color=AXIS, lw=0.8)
    ax_apr.set_title("Weekly system return (users + admin) as APR — profit / loss")
    ax_apr.set_ylabel("% APR")
    mark_eras(ax_apr, w, markets)

    ax_vol.plot(wk.index + pd.Timedelta(days=3.5), wk["vol"] * 100, color=INK2, lw=1.2,
                marker="o", ms=3)
    ax_vol.set_title("BTC realised volatility, weekly (1h returns, annualised)")
    ax_vol.set_ylabel("%")
    ax_vol.set_ylim(bottom=0)
    mark_eras(ax_vol, w, markets)

    ax_vol.xaxis.set_major_locator(mdates.MonthLocator())
    ax_vol.xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
    fig.tight_layout()
    save(fig, "btc_fundamental_pnl")
    return fig


def fig_by_market(markets, w):
    assets = ["WBTC", "cbBTC", "tBTC"]
    fig, axes = plt.subplots(2, 3, figsize=(13, 7), sharex=True, sharey="row")
    for col, asset in enumerate(assets):
        ax_p, ax_e = axes[0, col], axes[1, col]
        for idx, m in sorted(markets.items()):
            if m["symbol"] != asset:
                continue
            c = VERSION_COLOR[m["version"]]
            live = w[f"e_adj_{idx}"] > 1e-3
            ax_p.plot(w.index[live], pct(w[f"idx_adj_{idx}"])[live], color=c, lw=1.3,
                      label=f"{m['version']} (market {idx})")
            ax_e.plot(w.index, w[f"e_adj_{idx}"], color=c, lw=1.3)
        ax_p.axhline(0, color=AXIS, lw=0.8)
        ax_p.set_title(f"{asset}: relative PnL (users + admin)")
        ax_e.set_title(f"{asset}: equity (users + admin)")
        ax_p.legend(loc="upper left")
        for ax in (ax_p, ax_e):
            mark_eras(ax, w, markets)
    axes[0, 0].set_ylabel("cumulative return, %")
    axes[1, 0].set_ylabel("BTC")
    for ax in axes[1]:
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[1, 3, 5, 7, 9, 11]))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
    fig.tight_layout()
    save(fig, "btc_pnl_by_market")
    return fig


def fig_valuations(markets, w):
    fig = plt.figure(figsize=(12, 11.5))
    gs = fig.add_gridspec(3, 3, height_ratios=[1.8, 0.8, 1])
    ax = fig.add_subplot(gs[0, :])
    lines = (
        ("idx_raw_total", MUTED, 1.1, "-", "LT books: LP at price_scale, actual virtual_price, USD agg"),
        ("idx_adj_total", INK, 2.0, "-", "as designed: LP at price_scale, guaranteed virtual_price"),
        ("idx_rel_total", INK, 1.3, (0, (4, 2.5)), "reliable: as designed; when off, held at max(start, non-inflated)"),
        ("idx_mkt_total", MKT, 0.9, (0, (1, 1.5)), "mark-to-market: pool balances at the oracle price"),
        ("idx_bal_total", BAL, 1.6, "-", "balanced out gradually at the market price (non-inflated)"),
    )
    # (the one-jump bound, idx_jump_total, is in the data but not drawn: with large
    # price_scale gaps it pushes the LEVAMM out of its safe range and means nothing)
    ends = []
    for col, color, lw, ls, label in lines:
        y = pct(w[col])
        ax.plot(w.index, y, color=color, lw=lw, ls=ls, label=label)
        ends.append((y.iloc[-1], f"{y.iloc[-1]:+.1f}%", color, {}))
    label_ends(ax, w, ends)
    ax.axhline(0, color=AXIS, lw=0.8)
    ax.set_title(f"Relative PnL of all YB BTC markets (users + admin) under each valuation, {since(w)}")
    ax.set_ylabel("cumulative return, %")
    ax.legend(loc="lower left", fontsize=8)
    mark_eras(ax, w, markets, label=True)

    ax_c = fig.add_subplot(gs[1, :], sharex=ax)
    ax_c.fill_between(w.index, 0, w["bal_cost_total"] * 100, color=BAL_LIGHT, lw=0)
    ax_c.plot(w.index, w["bal_cost_total"] * 100, color=BAL, lw=1.0)
    ax_c.set_title("Latent cost of balancing out (book in crvUSD − balanced-out value), % of equity")
    ax_c.set_ylabel("% of equity")
    ax_c.set_ylim(bottom=min(0, (w["bal_cost_total"] * 100).min()))
    mark_eras(ax_c, w, markets)

    pool_of = {"v1": "v1/v2 pool (twocrypto v2.1.0d)", "v2": "v1/v2 pool (twocrypto v2.1.0d)",
               "v3": "v3 pool (twocrypto v3.0.0)"}
    for col, asset in enumerate(["WBTC", "cbBTC", "tBTC"]):
        a = fig.add_subplot(gs[2, col], sharex=ax)
        for v in ("v1", "v3"):  # v1 and v2 markets share one pool; v1 spans its whole life
            idx = next(i for i, m in markets.items() if m["symbol"] == asset and m["version"] == v)
            a.plot(w.index, w[f"ps_dev_{idx}"] * 100, color=VERSION_COLOR["v2" if v == "v1" else v],
                   lw=1.1, label=pool_of[v])
        a.axhline(0, color=AXIS, lw=0.8)
        a.set_title(f"{asset}: price_scale vs oracle")
        a.set_ylabel("% above oracle" if col == 0 else "")
        a.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[1, 4, 7, 10]))
        a.xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
        if col == 0:
            a.legend(loc="upper left", fontsize=8)
        mark_eras(a, w, markets)
    fig.tight_layout()
    save(fig, "btc_pnl_valuations")
    return fig


def version_zones(w, markets):
    """[(start, end, version)] spans where `version` holds most of the group's equity."""
    versions = sorted({m["version"] for m in markets.values()})
    eq = pd.DataFrame({v: sum(w[f"e_adj_{i}"] for i, m in markets.items() if m["version"] == v)
                       for v in versions})
    dom = eq.idxmax(axis=1).where(eq.sum(axis=1) > 0)
    change = dom.ne(dom.shift()).to_numpy()
    starts = np.flatnonzero(change)
    ends = np.r_[starts[1:], len(dom)] - 1
    return [(w.index[a], w.index[b], dom.iloc[a]) for a, b in zip(starts, ends)
            if isinstance(dom.iloc[a], str)]


def fig_relative(w, markets, group, unit, title, name, threshold):
    """One panel: relative PnL of a group of markets, system and users' part (as
    designed and reliable)."""
    live = w[f"e_adj_{group}"] > 0
    w = w.loc[w.index >= w.index[live.to_numpy().argmax()]]
    system, reliable = pct(w[f"idx_adj_{group}"]), pct(w[f"idx_rel_{group}"])
    users, users_rel = pct(w[f"idx_users_{group}"]), pct(w[f"idx_users_rel_{group}"])
    fig, ax = plt.subplots(figsize=(11, 5))
    labelled = set()
    for a, b, v in version_zones(w, markets):
        ax.axvspan(a, b, color=VERSION_TINT[v], lw=0, zorder=0,
                   label=None if v in labelled else f"most TVL in {v}")
        labelled.add(v)
    ax.plot(w.index, system, color=INK, lw=2.0,
            label="system = users + admin fee, pools as designed")
    ax.plot(w.index, reliable, color=INK, lw=1.4, ls=(0, (4, 2.5)),
            label=f"reliable: while price_scale is >{threshold:.0%} off, "
                  "held at max(start, non-inflated value)")
    ax.plot(w.index, users, color=USERS, lw=1.4, label="users' part (after admin fee)")
    ax.plot(w.index, users_rel, color=USERS, lw=1.2, ls=(0, (4, 2.5)),
            label="users' part, reliable")
    ax.axhline(0, color=AXIS, lw=0.8)
    launch = {}
    for m in markets.values():  # first market of each version in this group
        launch[m["version"]] = min(launch.get(m["version"], m["created_block"]), m["created_block"])
    for version, block in sorted(launch.items(), key=lambda kv: kv[1])[1:]:
        t = w.index[min(w["block"].searchsorted(block), len(w) - 1)]
        ax.axvline(t, color=AXIS, lw=0.8, zorder=0)
        ax.annotate(f"{version} launch", (t, 1), xycoords=("data", "axes fraction"),
                    xytext=(3, -2), textcoords="offset points", va="top", color=MUTED, fontsize=8)
    ax.set_title(f"Relative fundamental PnL: {title}, {since(w)}")
    ax.set_ylabel(f"cumulative return, % of {unit} equity")
    handles, labels = ax.get_legend_handles_labels()
    order = [i for i, lab in enumerate(labels) if not lab.startswith("most TVL")]
    order += sorted((i for i in range(len(labels)) if i not in order), key=lambda i: labels[i])
    ax.legend([handles[i] for i in order], [labels[i] for i in order],
              loc="upper left", bbox_to_anchor=(0, 0.93), fontsize=8.5, ncol=2,
              frameon=True, facecolor=SURFACE, edgecolor=GRID, framealpha=1)
    lines = (system, reliable, users, users_rel)
    lo, hi = min(min(s.min() for s in lines), 0), max(s.max() for s in lines)
    ax.set_ylim(lo - 0.05 * (hi - lo), hi + 0.55 * (hi - lo))  # headroom for the legend
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
    fig.tight_layout()
    label_ends(ax, w, [
        (system.iloc[-1], f"{system.iloc[-1]:+.2f}%", INK, {"fontweight": "bold"}),
        (reliable.iloc[-1], f"{reliable.iloc[-1]:+.2f}%", INK, {}),
        (users.iloc[-1], f"{users.iloc[-1]:+.2f}%", USERS, {"fontweight": "bold"}),
        (users_rel.iloc[-1], f"{users_rel.iloc[-1]:+.2f}%", USERS, {})])
    save(fig, name)
    return fig


def fig_apr_vs_vol(wk):
    fig, ax = plt.subplots(figsize=(7.5, 5))
    lagging = wk["ps_lag"] > LAG_THRESHOLD
    for mask, color, label in (
            (~lagging, POS, f"pools' price_scale within {LAG_THRESHOLD:.0%} of market"),
            (lagging, VERSION_COLOR["v2"], f"pools lagging the market by >{LAG_THRESHOLD:.0%}")):
        s = wk[mask]
        med = s["apr"].median() * 100
        ax.scatter(s["vol"] * 100, s["apr"] * 100, s=40, color=color, edgecolor=SURFACE,
                   linewidth=1.5, zorder=3, label=f"{label}  (n={len(s)}, median {med:+.1f}%)")
    for t, r in wk[wk["apr"] < -0.3].iterrows():  # the price_scale catch-up weeks
        ax.annotate(t.strftime("week of %d %b %Y"), (r["vol"] * 100, r["apr"] * 100),
                    xytext=(6, 0), textcoords="offset points", va="center",
                    color=INK2, fontsize=8)
    ax.axhline(0, color=AXIS, lw=0.8)
    ax.set_xlim(left=0)
    ax.set_xlabel("BTC realised volatility, weekly (% annualised)")
    ax.set_ylabel("weekly system return, users + admin (% APR)")
    ax.set_title("Weekly profitability vs volatility and pool tracking")
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    save(fig, "btc_apr_vs_vol")
    return fig


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", action="store_true", help="open interactive (QtAgg) windows")
    ap.add_argument("--dev-threshold", type=float, default=0.02)
    args = ap.parse_args()

    markets, w = load()
    btc = {i: m for i, m in markets.items() if is_btc(m["symbol"])}
    wk = weekly(w, btc)
    wk.to_csv(DATA / "weekly.csv", float_format="%.6g")
    fig_main(btc, w, wk, args.dev_threshold)
    fig_by_market(btc, w)
    fig_valuations(btc, w)
    fig_apr_vs_vol(wk)
    fig_relative(w, btc, "total", "BTC", "all YB BTC markets (v1 → v2 → v3, equity-weighted)",
                 "btc_relative_pnl", args.dev_threshold)
    for sym in ("WBTC", "cbBTC", "tBTC", "WETH"):
        ms = {i: m for i, m in markets.items() if m["symbol"] == sym}
        versions = " → ".join(f"{m['version']} (market {i})" for i, m in sorted(ms.items()))
        fig_relative(w, ms, sym, "ETH" if sym == "WETH" else "BTC", f"{sym} markets, {versions}",
                     f"{sym.lower()}_relative_pnl", args.dev_threshold)
    if args.show:
        plt.show()


if __name__ == "__main__":
    main()
