"""Cross-check the valuation against the contracts themselves.

1. Offline, every sampled row: the python LEVAMM math (yb_common.amm_value on
   lp_price(virtual_price, price_scale) * agg) must equal AMM.value_oracle()
   bit-for-bit. compute_pnl.py relies on this to re-price the LP at the
   guaranteed virtual price.

2. boa mainnet fork at a few historical blocks: make each v2/v3 LT checkpoint
   its books (prank the staker -> checkpoint_staker_rebase) and confirm
   liquidity.total + liquidity.admin (users + admin) equals our E_raw. This is
   what makes E the *fundamental* value: the sum of the users' and the admin's
   parts. (v1 LT books at price_oracle instead of price_scale, so it is skipped.)

Usage:
    uv run python validate.py [--blocks 5]
"""
from __future__ import annotations

import argparse
import json

import boa
import numpy as np
import pandas as pd

from yb_common import DATA, WAD, amm_value, lp_price, rpc_url

LT_ABI = json.dumps([
    {"name": "checkpoint_staker_rebase", "type": "function", "stateMutability": "nonpayable",
     "inputs": [], "outputs": []},
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "tuple", "components": [
         {"name": "admin", "type": "int256"}, {"name": "total", "type": "uint256"},
         {"name": "ideal_staked", "type": "uint256"}, {"name": "staked", "type": "uint256"}]}]},
])
AMM_ABI = json.dumps([
    {"name": "value_oracle", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "tuple", "components": [
         {"name": "p_o", "type": "uint256"}, {"name": "value", "type": "uint256"}]}]},
])
POOL_ABI = json.dumps([
    {"name": "price_scale", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint256"}]},
])
AGG_ABI = json.dumps([
    {"name": "price_w", "type": "function", "stateMutability": "nonpayable", "inputs": [],
     "outputs": [{"name": "", "type": "uint256"}]},
])


def check_offline(states: pd.DataFrame, markets: dict) -> None:
    n = bad = 0
    for r in states.itertuples(index=False):
        if r.vo_value == "" or r.price_scale == "":
            continue
        p_lp = lp_price(int(r.virtual_price), int(r.price_scale)) * int(r.agg_price) // WAD
        v = amm_value(p_lp, int(r.collateral), int(r.debt), markets[int(r.market)]["leverage"])
        n += 1
        bad += (v != int(r.vo_value)) or (p_lp != int(r.vo_p))
    print(f"[offline] python value == AMM.value_oracle on {n - bad:,}/{n:,} rows")


def check_fork(states: pd.DataFrame, markets: dict, n_blocks: int) -> None:
    lt_f, amm_f = boa.loads_abi(LT_ABI, name="LT"), boa.loads_abi(AMM_ABI, name="AMM")
    pool_f, agg_f = boa.loads_abi(POOL_ABI, name="Pool"), boa.loads_abi(AGG_ABI, name="Agg")
    states = states[states["market"].astype(int).map(lambda i: markets[i]["version"] != "v1")]
    states = states[pd.to_numeric(states["collateral"], errors="coerce") > 0]
    rng = np.random.default_rng(1)
    blocks = sorted(rng.choice(states["block"].astype(int).unique(), n_blocks, replace=False))
    for block in blocks:
        with boa.fork(rpc_url(), block_identifier=int(block)):  # fresh fork per block
            for idx in states.loc[states["block"].astype(int) == block, "market"].astype(int):
                m = markets[idx]
                lt = lt_f.at(m["lt"])
                with boa.env.prank(m["staker"]):
                    lt.checkpoint_staker_rebase()
                admin, total, *_ = lt.liquidity()
                booked = (total + admin) / WAD
                # Same block, after the checkpoint: agg.price_w() was refreshed by it
                ps = pool_f.at(m["cryptopool"]).price_scale()
                agg = agg_f.at(m["agg"]).price_w()
                e_raw = amm_f.at(m["amm"]).value_oracle()[1] * WAD // (ps * agg // WAD) / WAD
                print(f"[fork @{block}] market {idx} {m['version']} {m['symbol']:6s} "
                      f"LT total+admin {booked:12.6f} BTC   E_raw {e_raw:12.6f} BTC   "
                      f"rel.diff {abs(booked - e_raw) / e_raw:.1e}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--blocks", type=int, default=5)
    args = ap.parse_args()
    markets = {m["idx"]: m for m in json.loads((DATA / "markets.json").read_text())}
    states = pd.read_csv(DATA / "states.csv.xz", dtype=str, keep_default_na=False)
    check_offline(states, markets)
    check_fork(states, markets, args.blocks)


if __name__ == "__main__":
    main()
