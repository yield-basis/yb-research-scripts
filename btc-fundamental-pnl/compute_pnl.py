"""Fundamental PnL of the YB BTC markets: users + admin together, relative and in BTC.

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

Admin fee charged: LT._calculate_values is replayed exactly at every sample
(checked against pricePerShare()), giving the admin's accrued claim; its
increments, plus what WithdrawAdminFees converts into fee-receiver shares, are
the admin fee charged. Users' part = system PnL - admin fee charged.

Intervals: PnL is the sum of dE over sample intervals with no flow of that market
(AMM AddLiquidityRaw / RemoveLiquidityRaw). Every flow block b is sampled at b-1
and b, so exactly the single block (b-1, b] is dropped and nothing else.

Relative PnL: an equity-weighted, chain-linked (time-weighted) return index over
all BTC markets: per interval r = sum(dE) / sum(E_start) over the markets valid in
that interval, index = prod(1 + r). This is the growth of 1 BTC kept pro rata in
the BTC markets of the time (v1 -> v2 -> v3), independent of TVL swings.

Outputs:
    data/pnl.csv.xz      per sampled block: per-market equity, cumulative BTC PnL,
                         admin fee charged, return indexes, price_scale lag, BTC price
    data/monthly.csv     per calendar month: returns (system / users / admin), BTC PnL,
                         mean equity, realised vol (Chainlink BTC/USD, 1h returns)

Usage:
    uv run python compute_pnl.py
"""
from __future__ import annotations

import csv
import json
import lzma

import numpy as np
import pandas as pd

from yb_common import (DATA, SECONDS_PER_YEAR, WAD, amm_value, lp_price, lt_calculate_values,
                       realised_vol, vp_floor)

ZERO = "0x0000000000000000000000000000000000000000"


def load_markets() -> dict[int, dict]:
    return {m["idx"]: m for m in json.loads((DATA / "markets.json").read_text())}


def value_rows(states: pd.DataFrame, markets: dict[int, dict]) -> pd.DataFrame:
    """Exact integer valuation and LT admin replay per (block, market) row."""
    cols = ("e_raw", "e_adj", "e_mkt", "admin", "ok")
    out = {k: [] for k in cols}
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
        out["ok"].append(True)

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
    return pd.DataFrame(out, index=states.index)


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


def per_market(states, markets, flows, admin_resets) -> pd.DataFrame:
    """Per (block, market): equity, PnL increment d_*, TWR weight w_*, admin increment."""
    frames = []
    for idx, g in states.groupby("market", sort=True):
        g = g.sort_values("block")
        blocks = g["block"].to_numpy()
        is_flow = np.isin(blocks, list(flows.get(idx, ())))
        is_reset = np.isin(blocks, list(admin_resets.get(idx, ())))
        ok = g["ok"].to_numpy(dtype=bool)
        f = pd.DataFrame({"block": blocks, "market": idx})
        for v in ("raw", "adj", "mkt"):
            e = g[f"e_{v}"].to_numpy()
            prev = np.r_[np.nan, e[:-1]]
            valid = ~is_flow & ok & np.r_[False, ok[:-1]] & (prev > 0)
            f[f"e_{v}"] = e
            f[f"d_{v}"] = np.where(valid, e - prev, 0.0)
            f[f"w_{v}"] = np.where(valid, prev, 0.0)
            if v == "adj":
                a = g["admin"].to_numpy()
                # at WithdrawAdminFees the accrued claim is moved into fee-receiver
                # shares (still charged), so only what accrued after counts as new
                da = np.where(is_reset, a, a - np.r_[np.nan, a[:-1]])
                f["d_admin"] = np.where(valid & np.isfinite(da), da, 0.0)
        frames.append(f)
        m = markets[idx]
        print(f"  [{idx}] {m['version']} {m['symbol']:6s} PnL adj {f.d_adj.sum():+8.3f}  "
              f"raw {f.d_raw.sum():+8.3f}  mkt {f.d_mkt.sum():+8.3f} BTC   "
              f"of which admin fee {f.d_admin.sum():+7.3f} BTC   "
              f"dropped {is_flow.sum():5d} flow blocks")
    return pd.concat(frames)


def main() -> None:
    markets = load_markets()
    states = pd.read_csv(DATA / "states.csv.xz", dtype=str, keep_default_na=False)
    for c in ("block", "timestamp", "market"):
        states[c] = states[c].astype(int)
    states = states.sort_values(["market", "block"]).reset_index(drop=True)
    print(f"{len(states):,} state rows, {states.block.nunique():,} blocks")

    flows, admin_resets = load_events()
    states = states.join(value_rows(states, markets))
    for col in ("price_scale", "price_oracle"):
        states[col] = pd.to_numeric(states[col], errors="coerce") / WAD
    pm = per_market(states, markets, flows, admin_resets)

    # ---- wide table on the common block axis
    times = states.groupby("block")["timestamp"].first()
    wide = pd.DataFrame({"timestamp": times})
    piv = {c: pm.pivot(index="block", columns="market", values=c).reindex(wide.index)
           for c in pm.columns if c[:2] in ("e_", "d_", "w_")}
    for c in piv:
        piv[c] = piv[c].ffill().fillna(0.0) if c.startswith("e_") else piv[c].fillna(0.0)

    groups = {"total": list(markets)}
    groups |= {v: [i for i in markets if markets[i]["version"] == v] for v in ("v1", "v2", "v3")}
    groups |= {str(i): [i] for i in markets}
    new = {}
    for name, idx in groups.items():
        for v in ("raw", "adj", "mkt"):
            d, w = piv[f"d_{v}"][idx].sum(axis=1), piv[f"w_{v}"][idx].sum(axis=1)
            r = (d / w.where(w > 0)).fillna(0.0)
            new[f"e_{v}_{name}"] = piv[f"e_{v}"][idx].sum(axis=1)
            new[f"pnl_{v}_{name}"] = d.cumsum()            # BTC
            new[f"r_{v}_{name}"] = r
            new[f"idx_{v}_{name}"] = (1 + r).cumprod()     # relative, system
        d_adm = piv["d_admin"][idx].sum(axis=1)
        w_adj = piv["w_adj"][idx].sum(axis=1)
        r_adm = (d_adm / w_adj.where(w_adj > 0)).fillna(0.0)
        new[f"admin_{name}"] = d_adm.cumsum()              # BTC charged to admin
        new[f"r_admin_{name}"] = r_adm
        new[f"idx_users_{name}"] = (1 + new[f"r_adj_{name}"] - r_adm).cumprod()
    wide = pd.concat([wide, pd.DataFrame(new)], axis=1)

    # How far each market's pool price_scale sits from its EMA price oracle
    dev = states.assign(dev=states["price_scale"] / states["price_oracle"] - 1)
    dev = dev.pivot(index="block", columns="market", values="dev").reindex(wide.index).ffill()
    wide = wide.join(dev.add_prefix("ps_dev_"))
    # BTC price: Chainlink BTC/USD (external; pool prices go stale in quiet weeks)
    wide["btc_usd"] = pd.to_numeric(states.groupby("block")["chainlink_btcusd"].first()) / 1e8
    wide.reset_index().to_csv(DATA / "pnl.csv.xz", index=False, float_format="%.10g")

    # ---- monthly summary
    w = wide.copy()
    w["dt"] = pd.to_datetime(w["timestamp"], unit="s", utc=True)
    w["month"] = w["dt"].dt.strftime("%Y-%m")
    dt_s = w["timestamp"].diff().shift(-1).fillna(0)
    end = w.groupby("month")[["pnl_adj_total", "admin_total"]].last()
    btc = end.diff().fillna(end)
    rows = []
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
            "pnl_btc": btc.loc[month, "pnl_adj_total"],
            "admin_fee_btc": btc.loc[month, "admin_total"],
            "mean_equity_btc": (g["e_adj_total"] * dt_s.loc[g.index]).sum() / max(secs, 1),
            "btc_usd_end": px.iloc[-1], "btc_return": px.iloc[-1] / px.iloc[0] - 1,
            "realised_vol": realised_vol(px),
        })
    monthly = pd.DataFrame(rows)
    monthly.to_csv(DATA / "monthly.csv", index=False, float_format="%.6g")

    pd.set_option("display.width", 220)
    print()
    print(monthly.to_string(index=False, float_format=lambda x: f"{x:,.4f}"))
    last = wide.iloc[-1]
    print(f"\nRelative, all BTC markets (as designed): system {last.idx_adj_total - 1:+.2%}, "
          f"users {last.idx_users_total - 1:+.2%}, admin fee {last.idx_adj_total - last.idx_users_total:+.2%}"
          f"   [LT books {last.idx_raw_total - 1:+.2%}, mark-to-market {last.idx_mkt_total - 1:+.2%}]")
    print(f"Absolute: system {last.pnl_adj_total:+.3f} BTC, of which admin fee charged "
          f"{last.admin_total:+.3f} BTC")
    print(f"-> {DATA / 'pnl.csv.xz'}, {DATA / 'monthly.csv'}")


if __name__ == "__main__":
    main()
