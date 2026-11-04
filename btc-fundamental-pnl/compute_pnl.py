"""Fundamental PnL of the YB markets: users + admin together, relative and in the asset.

Groups: "total" and v1/v2/v3 are over the BTC markets only (the BTC "system");
per-asset groups (WBTC, cbBTC, tBTC, WETH) chain each asset's markets across
versions; every market also has its own columns (suffix = market index).

Equity of a market at a sampled block (asset units = BTC):
    E_raw  = value_oracle / LT._price_oracle()          (LT's own cur_value)
    E_adj  = LP priced at the pool's guaranteed virtual price (vp_floor, see
             yb_common) and everything in crvUSD (agg = 1) -> "pools as designed"
    E_mkt  = (collateral * LP_mkt - debt) / price_oracle, where
             LP_mkt = (balances[0] + balances[1] * price_oracle) / totalSupply
             -> mark-to-market of what the LP really holds
Each is the *whole* market: what LT splits into users' liquidity.total and the
admin's liquidity.admin. (agg = 1 for E_adj: LT prices the LP in USD but the debt
in crvUSD, so a crvUSD depeg moves its books by ~(1 - agg) * debt / price although
the position is crvUSD-neutral.)

    E_book = LP at the pool's actual virtual price, at price_scale, crvUSD units
    E_bal  = what the market would be worth after balancing out at today's price:
             price_scale walked to the market price in small steps, pool and
             LEVAMM arbitraged after each, with the fees those trades leave behind
             (see rebalance.py) -> non-inflated value
    E_jump = the same with price_scale moved in one jump (pessimistic bound)

Reliable line: equal to the as-designed value whenever a market's pool is
balanced (price_scale within --dev-threshold, 2%, of its oracle). While it is
further off, the as-designed measurement is inflated, so that market's value is
held at max(value when the deviation started, non-inflated value), the
non-inflated value being E_bal chained from the window start; when the pool is
back within the threshold the line rejoins the as-designed value. A group's line
is its as-designed index times the equity-weighted average of its markets'
factors (reliable / as-designed value): no path dependence, equal to the
as-designed line whenever every pool is balanced, and always between its
markets' own lines.

Admin fee charged: LT._calculate_values is replayed exactly at every sample
(checked against pricePerShare()), giving the admin's accrued claim; its
increments, plus what WithdrawAdminFees converts into fee-receiver shares, are
the admin fee charged. Users' part = system PnL - admin fee charged.

What users earned, in BTC, three ways (pnl_users*_<group> columns, summary.csv):
    as designed   system (E_adj) PnL - admin fee charged
    reliable      reliable-line PnL (E_adj * reliable factor) - admin fee charged
    LT books      E_raw PnL - admin fee charged = the change of LT's own
                  liquidity.total, i.e. what pricePerShare() reports. Exact for
                  v2/v3, whose LT books at price_scale; v1 LT books at the EMA
                  price_oracle, so for v1 it is distorted whenever price_scale
                  and the oracle differ. Use the as-designed / reliable numbers
                  there.

Window: everything is measured from the state at --start (default
2025-10-15 12:02:22 UTC, the last block at or before it; fetch_states.py samples
that block). Only intervals that begin at or after it count, and the outputs
start there (index = 1, PnL = 0).

Intervals: PnL is the sum of dE over sample intervals with no flow of that market
(AMM AddLiquidityRaw / RemoveLiquidityRaw). Every flow block b is sampled at b-1
and b, so exactly the single block (b-1, b] is dropped and nothing else.

Relative PnL: an equity-weighted, chain-linked (time-weighted) return index over
all BTC markets: per interval r = sum(dE) / sum(E_start) over the markets valid in
that interval, index = prod(1 + r). This is the growth of 1 BTC kept pro rata in
the BTC markets of the time (v1 -> v2 -> v3), independent of TVL swings.

Outputs:
    data/pnl.csv.xz      per sampled block from the window start: per-market equity,
                         cumulative BTC PnL (system, users, admin fee charged),
                         return indexes, price_scale lag, BTC price
    data/monthly.csv     per calendar month: returns (system / users / admin), BTC PnL,
                         mean equity, realised vol (Chainlink BTC/USD, 1h returns)
    data/summary.csv     per group (all BTC, v1/v2/v3, each asset): what users earned
                         over the whole window, in the asset and relative

Usage:
    uv run python compute_pnl.py [--start "2025-10-15 12:02:22"] [--dev-threshold 0.02]
"""
from __future__ import annotations

import argparse
import csv
import json
import lzma

import numpy as np
import pandas as pd

from rebalance import ann, balance_out, ss_D
from yb_common import (DATA, SECONDS_PER_YEAR, START, WAD, amm_value, is_btc, lp_price,
                       lt_calculate_values, parse_utc, realised_vol, vp_floor)

VALUATIONS = ("adj", "raw", "mkt", "book", "bal", "jump")
FEE_PRECISION = 10**10  # twocrypto mid_fee / out_fee units
BALANCE_STEPS = 40      # price_scale walk to the market price (40 vs 80: < 0.05% apart)

ZERO = "0x0000000000000000000000000000000000000000"


def load_markets() -> dict[int, dict]:
    return {m["idx"]: m for m in json.loads((DATA / "markets.json").read_text())}


def value_rows(states: pd.DataFrame, markets: dict[int, dict]) -> pd.DataFrame:
    """Exact integer valuation and LT admin replay per (block, market) row."""
    cols = ("e_raw", "e_adj", "e_mkt", "e_book", "admin", "ok")
    out = {k: [] for k in cols}
    bal_in = {k: [] for k in ("row", "b0", "b1", "p", "amp", "pool_fee", "supply", "donation",
                              "vp", "floor", "coll", "debt", "lev", "amm_fee", "ps", "D")}
    n_pps = bad_vo = bad_pps = 0
    for r in states.itertuples(index=False):
        m = markets[r.market]
        try:
            ps, vp, xcp, agg = int(r.price_scale), int(r.virtual_price), int(r.xcp_profit), int(r.agg_price)
            coll, debt, po = int(r.collateral), int(r.debt), int(r.price_oracle)
            b0, b1, supply = int(r.cp_balance0), int(r.cp_balance1), int(r.cp_supply)
            lp_xcp = int(r.lp_xcp_profit) if m["version"] == "v3" else None
        except ValueError:  # a failed sub-call (blank field)
            for k in cols:
                out[k].append(False if k == "ok" else np.nan)
            continue

        p_lp = lp_price(vp, ps) * agg // WAD      # CryptopoolLPOracle.price()
        v_raw = amm_value(p_lp, coll, debt, m["leverage"])
        bad_vo += r.vo_value != "" and v_raw != int(r.vo_value)
        p_lt = (po if m["version"] == "v1" else ps) * agg // WAD   # LT._price_oracle()
        floor = vp_floor(m["version"], xcp, lp_xcp)
        v_adj = amm_value(lp_price(floor, ps), coll, debt, m["leverage"])
        lp_mkt = (b0 + b1 * 10**(18 - m["decimals"]) * po // WAD) * WAD // supply if supply else 0

        out["e_raw"].append(v_raw * WAD // p_lt / WAD)
        out["e_adj"].append(v_adj * WAD // ps / WAD)
        out["e_mkt"].append((coll * lp_mkt // WAD - debt) * WAD // po / WAD)
        v_book = amm_value(lp_price(vp, ps), coll, debt, m["leverage"])
        out["e_book"].append(v_book * WAD // ps / WAD)
        out["ok"].append(True)

        if supply and r.cp_A != "" and coll:  # inputs for the rebalanced valuation
            fee = max(int(r.cp_mid_fee), int(r.cp_out_fee))  # flat in practice (mid == out)
            for k, v in (("row", len(out["ok"]) - 1), ("b0", b0 / WAD),
                         ("b1", b1 / 10**m["decimals"]), ("p", po / WAD), ("amp", int(r.cp_A)),
                         ("pool_fee", fee / FEE_PRECISION), ("supply", supply / WAD),
                         ("donation", int(r.cp_donation_shares) / WAD), ("vp", vp / WAD),
                         ("floor", floor / WAD), ("coll", coll / WAD), ("debt", debt / WAD),
                         ("lev", m["leverage"] / WAD), ("amm_fee", int(r.amm_fee) / WAD),
                         ("ps", ps / WAD), ("D", int(r.cp_D) / WAD)):
                bal_in[k].append(v)

        # admin's accrued claim, replaying LT._calculate_values at this block
        admin = np.nan
        lt_supply = int(r.lt_supply or 0)
        if lt_supply == 0:
            admin = 0.0
        elif r.lt_staked_tokens != "" and r.min_admin_fee != "":
            staked = int(r.lt_staked_tokens) if r.lt_staker.lower() != ZERO else 0
            try:
                a, total, supply_tokens = lt_calculate_values(
                    int(r.lt_admin), int(r.lt_total), int(r.lt_ideal_staked), int(r.lt_staked),
                    staked, lt_supply, int(r.min_admin_fee), v_raw * WAD // p_lt)
                admin = a / WAD
                if r.lt_pps != "" and supply_tokens > 0:
                    n_pps += 1
                    bad_pps += total * WAD // supply_tokens != int(r.lt_pps)
            except ZeroDivisionError:
                pass
        out["admin"].append(admin)

    print(f"check: python value != AMM.value_oracle on {bad_vo} rows")
    print(f"check: replayed LT split == LT.pricePerShare on {n_pps - bad_pps:,}/{n_pps:,} rows")
    df = pd.DataFrame(out, index=states.index)

    # rebalanced (non-inflated) equity, vectorised over all rows at once
    x = {k: np.asarray(v, dtype=float) for k, v in bal_in.items()}
    d_now = ss_D(x["b0"], x["b1"] * x["ps"], ann(x["amp"]))
    err = np.abs(d_now / x["D"] - 1)
    print(f"check: float stableswap D vs pool.D(): median rel.err {np.median(err):.1e}, "
          f"99.9% {np.quantile(err, 0.999):.1e}")
    kw = dict(b0=x["b0"], b1=x["b1"], ps=x["ps"], p=x["p"], amp=x["amp"],
              pool_fee=x["pool_fee"], supply=x["supply"], donation_shares=x["donation"],
              vp=x["vp"], vp_floor=x["floor"], coll=x["coll"], debt=x["debt"],
              leverage=x["lev"], amm_fee=x["amm_fee"])
    rows = df.index[x["row"].astype(int)]
    for col, steps in (("e_bal", BALANCE_STEPS), ("e_jump", 1)):
        df[col] = np.nan
        df.loc[rows, col] = balance_out(**kw, steps=steps)["e_bal"]
        df.loc[df["ok"] & df[col].isna(), col] = 0.0  # empty AMM
    return df


def load_events():
    flows: dict[int, set[int]] = {}
    admin_resets: dict[int, set[int]] = {}
    with lzma.open(DATA / "events.csv.xz", "rt") as fh:
        for r in csv.DictReader(fh):
            if r["contract"] == "amm":
                flows.setdefault(int(r["market"]), set()).add(int(r["block"]))
            elif r["event"] == "WithdrawAdminFees":
                admin_resets.setdefault(int(r["market"]), set()).add(int(r["block"]))
    return flows, admin_resets


def reliable_factor(r: np.ndarray, r_true: np.ndarray, unreliable: np.ndarray) -> np.ndarray:
    """Reliable value / as-designed value, per sample.

    1 outside unreliable windows. Inside a window starting after sample k:
    M_k * max(1, B / B_k) / M, with M the as-designed index (returns r) and B the
    non-inflated one (returns r_true).
    """
    m, b = np.cumprod(1 + r), np.cumprod(1 + r_true)
    out = np.ones_like(m)
    anchor = None
    for i in range(len(r)):
        if unreliable[i]:
            if anchor is None:
                anchor = (m[i - 1], b[i - 1]) if i else (1.0, 1.0)
            out[i] = anchor[0] * max(1.0, b[i] / anchor[1]) / m[i]
        else:
            anchor = None
    return out


def per_market(states, markets, flows, admin_resets, dev_threshold, start_block) -> pd.DataFrame:
    """Per (block, market): equity, PnL increment d_*, TWR weight w_*, admin increment.

    Only intervals (prev, b] with prev >= start_block count: PnL is measured from
    the state at start_block.
    """
    frames = []
    for idx, g in states.groupby("market", sort=True):
        g = g.sort_values("block")
        blocks = g["block"].to_numpy()
        in_window = np.r_[-1, blocks[:-1]] >= start_block
        is_flow = np.isin(blocks, list(flows.get(idx, ())))
        is_reset = np.isin(blocks, list(admin_resets.get(idx, ())))
        ok = g["ok"].to_numpy(dtype=bool)
        prev_ok = np.r_[False, ok[:-1]]
        f = pd.DataFrame({"block": blocks, "market": idx})
        for v in VALUATIONS:
            e = g[f"e_{v}"].to_numpy()
            prev = np.r_[np.nan, e[:-1]]
            valid = in_window & ~is_flow & ok & prev_ok & (prev > 0)
            f[f"e_{v}"] = e
            f[f"d_{v}"] = np.where(valid, e - prev, 0.0)
            f[f"w_{v}"] = np.where(valid, prev, 0.0)
            if v == "adj":
                a = g["admin"].to_numpy()
                # at WithdrawAdminFees the accrued claim is moved into fee-receiver
                # shares (still charged), so only what accrued after counts as new
                da = np.where(is_reset, a, a - np.r_[np.nan, a[:-1]])
                f["d_admin"] = np.where(valid & np.isfinite(da), da, 0.0)
        # reliable value: as designed, but held at max(start, non-inflated) in
        # windows where this market's pool price_scale is far from its oracle
        dev = np.nan_to_num((g["price_scale"] / g["price_oracle"] - 1).abs().to_numpy())
        w_adj, w_bal = f["w_adj"].to_numpy(), f["w_bal"].to_numpy()
        r = np.divide(f["d_adj"].to_numpy(), w_adj, out=np.zeros_like(w_adj), where=w_adj > 0)
        r_bal = np.divide(f["d_bal"].to_numpy(), w_bal, out=np.zeros_like(w_bal), where=w_bal > 0)
        fac = reliable_factor(r, r_bal, dev > dev_threshold)
        # reliable value E_adj * factor, in the asset. Its increment
        # fac_i * dE_adj + (fac_i - fac_{i-1}) * E_adj_{i-1} equals the plain
        # difference on flow-free intervals, and still books a factor jump (the
        # line rejoining as designed) that happens to land on a flow interval.
        e_adj = f["e_adj"].to_numpy()
        e_prev = np.r_[np.nan, e_adj[:-1]]
        step = in_window & ok & prev_ok & (e_prev > 0)
        f["e_rel"] = e_adj * fac
        f["d_rel"] = np.where(step, fac * f["d_adj"].to_numpy()
                              + (fac - np.r_[1.0, fac[:-1]]) * np.nan_to_num(e_prev), 0.0)
        frames.append(f)
        m = markets[idx]
        unit = "BTC" if is_btc(m["symbol"]) else m["symbol"].removeprefix("W")
        adm = f.d_admin.sum()
        print(f"  [{idx:2d}] {m['version']} {m['symbol']:6s} system: adj {f.d_adj.sum():+8.3f}  "
              f"rel {f.d_rel.sum():+8.3f}  raw {f.d_raw.sum():+8.3f}  bal {f.d_bal.sum():+8.3f}   "
              f"admin fee {adm:+7.3f}   users: adj {f.d_adj.sum() - adm:+8.3f}  "
              f"rel {f.d_rel.sum() - adm:+8.3f}  LT books {f.d_raw.sum() - adm:+8.3f} {unit}   "
              f"dropped {(is_flow & in_window).sum():5d} flow blocks")
    return pd.concat(frames)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dev-threshold", type=float, default=0.02,
                    help="|price_scale/oracle - 1| above which a market's measurement is "
                         "treated as unreliable (default 0.02)")
    ap.add_argument("--start", default=START,
                    help=f"window start, UTC 'YYYY-MM-DD HH:MM:SS' or unix seconds (default {START})")
    args = ap.parse_args()
    markets = load_markets()
    states = pd.read_csv(DATA / "states.csv.xz", dtype=str, keep_default_na=False)
    for c in ("block", "timestamp", "market"):
        states[c] = states[c].astype(int)
    states = states.sort_values(["market", "block"]).reset_index(drop=True)
    print(f"{len(states):,} state rows, {states.block.nunique():,} blocks")

    # window start: the last sampled block at or before --start
    times = states.groupby("block")["timestamp"].first()
    start_ts = parse_utc(args.start)
    before = times.index[times.to_numpy() <= start_ts]
    start_block = int(before.max()) if len(before) else int(times.index.min())
    gap = start_ts - int(times[start_block])
    print(f"window from {args.start} UTC: state at block {start_block} ({gap}s before)")
    if gap > 60:
        print("  WARNING: that is not the block at --start; rerun fetch_states.py with this --start")

    flows, admin_resets = load_events()
    states = states.join(value_rows(states, markets))
    for col in ("price_scale", "price_oracle"):
        states[col] = pd.to_numeric(states[col], errors="coerce") / WAD
    pm = per_market(states, markets, flows, admin_resets, args.dev_threshold, start_block)

    # ---- wide table on the common block axis
    wide = pd.DataFrame({"timestamp": times})
    piv = {c: pm.pivot(index="block", columns="market", values=c).reindex(wide.index)
           for c in pm.columns if c[:2] in ("e_", "d_", "w_")}
    for c in piv:
        piv[c] = piv[c].ffill().fillna(0.0) if c.startswith("e_") else piv[c].fillna(0.0)

    btc_markets = [i for i in markets if is_btc(markets[i]["symbol"])]
    groups = {"total": btc_markets}
    groups |= {v: [i for i in btc_markets if markets[i]["version"] == v]
               for v in ("v1", "v2", "v3")}
    groups |= {sym: [i for i in markets if markets[i]["symbol"] == sym]
               for sym in sorted({m["symbol"] for m in markets.values()})}
    groups |= {str(i): [i] for i in markets}
    new = {}
    for name, idx in groups.items():
        for v in VALUATIONS + ("rel",):
            d = piv[f"d_{v}"][idx].sum(axis=1)
            new[f"e_{v}_{name}"] = piv[f"e_{v}"][idx].sum(axis=1)
            new[f"pnl_{v}_{name}"] = d.cumsum()            # BTC
            if v == "rel":  # its index is built from the market factors below
                continue
            w = piv[f"w_{v}"][idx].sum(axis=1)
            r = (d / w.where(w > 0)).fillna(0.0)
            new[f"r_{v}_{name}"] = r
            new[f"idx_{v}_{name}"] = (1 + r).cumprod()     # relative, system
        d_adm = piv["d_admin"][idx].sum(axis=1)
        w_adj = piv["w_adj"][idx].sum(axis=1)
        r_adm = (d_adm / w_adj.where(w_adj > 0)).fillna(0.0)
        admin = new[f"admin_{name}"] = d_adm.cumsum()      # BTC charged to admin
        new[f"r_admin_{name}"] = r_adm
        new[f"idx_users_{name}"] = (1 + new[f"r_adj_{name}"] - r_adm).cumprod()
        # reliable: as-designed index * equity-weighted average market factor
        # (sum of E_adj * factor over sum of E_adj)
        e_adj = new[f"e_adj_{name}"]
        avg = (new[f"e_rel_{name}"] / e_adj.where(e_adj > 0)).fillna(1.0)
        idx_rel = new[f"idx_rel_{name}"] = new[f"idx_adj_{name}"] * avg
        r_rel = new[f"r_rel_{name}"] = idx_rel.pct_change().fillna(0.0)
        new[f"idx_users_rel_{name}"] = (1 + r_rel - r_adm).cumprod()
        # users' part on LT's own books (pricePerShare), weighted by book equity
        w_raw = piv["w_raw"][idx].sum(axis=1)
        r_adm_raw = (d_adm / w_raw.where(w_raw > 0)).fillna(0.0)
        new[f"idx_users_raw_{name}"] = (1 + new[f"r_raw_{name}"] - r_adm_raw).cumprod()
        # what users earned, in the asset: system PnL minus admin fee charged
        for suffix, v in (("", "adj"), ("_rel", "rel"), ("_raw", "raw")):
            new[f"pnl_users{suffix}_{name}"] = new[f"pnl_{v}_{name}"] - admin
        # latent cost of balancing out now, as a fraction of book equity
        book = new[f"e_book_{name}"]
        new[f"bal_cost_{name}"] = (1 - new[f"e_bal_{name}"] / book.where(book > 0)).fillna(0.0)
    wide = pd.concat([wide, pd.DataFrame(new)], axis=1)

    # How far each market's pool price_scale sits from its EMA price oracle
    dev = states.assign(dev=states["price_scale"] / states["price_oracle"] - 1)
    dev = dev.pivot(index="block", columns="market", values="dev").reindex(wide.index).ffill()
    wide = wide.join(dev.add_prefix("ps_dev_"))
    # BTC price: Chainlink BTC/USD (external; pool prices go stale in quiet weeks)
    wide["btc_usd"] = pd.to_numeric(states.groupby("block")["chainlink_btcusd"].first()) / 1e8
    # the window: nothing before start_block counted, so here PnL = 0 and indexes = 1
    wide = wide.loc[wide.index >= start_block]
    wide.reset_index().to_csv(DATA / "pnl.csv.xz", index=False, float_format="%.10g")

    # ---- monthly summary
    w = wide.copy()
    w["dt"] = pd.to_datetime(w["timestamp"], unit="s", utc=True)
    w["month"] = w["dt"].dt.strftime("%Y-%m")
    dt_s = w["timestamp"].diff().shift(-1).fillna(0)
    end = w.groupby("month")[["pnl_adj_total", "admin_total", "pnl_users_total",
                              "pnl_users_rel_total", "pnl_users_raw_total"]].last()
    btc = end.diff().fillna(end)
    rows = []
    prev_rel = 1.0
    for month, g in w.groupby("month", sort=True):
        secs = dt_s.loc[g.index].sum()
        system = np.prod(1 + g["r_adj_total"]) - 1
        users = np.prod(1 + g["r_adj_total"] - g["r_admin_total"]) - 1
        px = g.set_index("dt")["btc_usd"]
        rows.append({
            "month": month, "return_system": system, "return_users": users,
            "return_admin": system - users,
            "apr_system": system * SECONDS_PER_YEAR / secs if secs else np.nan,
            "return_lt_books": np.prod(1 + g["r_raw_total"]) - 1,
            "return_mkt": np.prod(1 + g["r_mkt_total"]) - 1,
            "return_rebalanced": np.prod(1 + g["r_bal_total"]) - 1,
            "return_rebalanced_jump": np.prod(1 + g["r_jump_total"]) - 1,
            "return_reliable": g["idx_rel_total"].iloc[-1] / prev_rel - 1,
            "return_users_reliable": np.prod(1 + g["r_rel_total"] - g["r_admin_total"]) - 1,
            "pnl_btc": btc.loc[month, "pnl_adj_total"],
            "admin_fee_btc": btc.loc[month, "admin_total"],
            "pnl_users_btc": btc.loc[month, "pnl_users_total"],
            "pnl_users_reliable_btc": btc.loc[month, "pnl_users_rel_total"],
            "pnl_users_lt_books_btc": btc.loc[month, "pnl_users_raw_total"],
            "mean_equity_btc": (g["e_adj_total"] * dt_s.loc[g.index]).sum() / max(secs, 1),
            "btc_usd_end": px.iloc[-1], "btc_return": px.iloc[-1] / px.iloc[0] - 1,
            "realised_vol": realised_vol(px),
        })
        prev_rel = g["idx_rel_total"].iloc[-1]
    monthly = pd.DataFrame(rows)
    monthly.to_csv(DATA / "monthly.csv", index=False, float_format="%.6g")

    # ---- what users earned over the whole window, per group
    summary = window_summary(w, dt_s, groups)
    summary.to_csv(DATA / "summary.csv", index=False, float_format="%.6g")

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    print()
    print(monthly.to_string(index=False, float_format=lambda x: f"{x:,.4f}"))
    first, last = w.iloc[0], w.iloc[-1]
    days = (last["timestamp"] - first["timestamp"]) / 86400
    print(f"\nWindow {first['dt']:%Y-%m-%d %H:%M:%S} -> {last['dt']:%Y-%m-%d %H:%M:%S} UTC "
          f"({days:.1f} days), blocks {int(w.index[0])}..{int(w.index[-1])}")
    show = summary.set_index("group")
    for c in show.columns:
        if "return" in c:
            show[c] = show[c].map(lambda x: f"{x:+.2%}")
    print(show.to_string(float_format=lambda x: f"{x:+,.3f}"))
    print(f"\nAll BTC markets: rebalanced (non-inflated) {last.idx_bal_total - 1:+.2%}, "
          f"mark-to-market {last.idx_mkt_total - 1:+.2%}, books in crvUSD "
          f"{last.idx_book_total - 1:+.2%}; latent balancing cost now {last.bal_cost_total:.2%} "
          f"of equity. Reliable = held at max(start, non-inflated) while "
          f"|ps/oracle-1| > {args.dev_threshold:.0%}.")
    print(f"-> {DATA / 'pnl.csv.xz'}, {DATA / 'monthly.csv'}, {DATA / 'summary.csv'}")


SUMMARY_GROUPS = {"total": "all BTC", "v1": "v1 BTC", "v2": "v2 BTC", "v3": "v3 BTC",
                  "WBTC": "WBTC", "cbBTC": "cbBTC", "tBTC": "tBTC", "WETH": "WETH"}


def window_summary(w: pd.DataFrame, dt_s: pd.Series, groups) -> pd.DataFrame:
    """Per group, over the whole window: users' PnL (3 ways), admin fee, returns."""
    first, last = w.iloc[0], w.iloc[-1]
    secs = max(dt_s.sum(), 1)
    rows = []
    for name, label in SUMMARY_GROUPS.items():
        if name not in groups:
            continue
        rows.append({
            "group": label, "unit": "ETH" if name == "WETH" else "BTC",
            "markets": " ".join(map(str, groups[name])),
            "equity_start": first[f"e_adj_{name}"], "equity_end": last[f"e_adj_{name}"],
            "equity_mean": (w[f"e_adj_{name}"] * dt_s).sum() / secs,
            "users_pnl": last[f"pnl_users_{name}"],
            "users_pnl_reliable": last[f"pnl_users_rel_{name}"],
            "users_pnl_lt_books": last[f"pnl_users_raw_{name}"],
            "admin_fee": last[f"admin_{name}"],
            "system_pnl": last[f"pnl_adj_{name}"],
            "users_return": last[f"idx_users_{name}"] - 1,
            "users_return_reliable": last[f"idx_users_rel_{name}"] - 1,
            "users_return_lt_books": last[f"idx_users_raw_{name}"] - 1,
            "system_return": last[f"idx_adj_{name}"] - 1,
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    main()
