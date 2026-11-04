"""Sample on-chain state of all YB markets at a block grid + flow boundaries.

Sampled blocks:
    * a regular grid every --stride blocks (default 150 ≈ 30 min) for plotting
    * for every flow block b of any market (data/events.csv.xz): b-1 and b, so a
      flow is always isolated in a single-block interval (b-1, b] that the PnL
      step can drop exactly; same for WithdrawAdminFees blocks (admin reset).
    * the last block at or before --start (default yb_common.START), so the
      measured window begins exactly there.

Each sampled block is ONE eth_call to Multicall3.aggregate3 bundling, for every
market already created at that block:
    AMM:  collateral_amount, get_debt, value_oracle, fee
    LT:   liquidity (admin, total, ideal_staked, staked), totalSupply,
          staker, balanceOf(staker), min_admin_fee, pricePerShare
    pool: price_scale, price_oracle, last_prices, virtual_price, xcp_profit,
          lp_xcp_profit (v3 only), totalSupply, balances(0/1), donation_shares,
          D, A, gamma, mid_fee, out_fee, fee_gamma
                                                      (deduplicated: v1/v2 share pools)
    agg:  price                                       (deduplicated)
    Chainlink BTC/USD latestAnswer                    (external BTC price, for volatility)
    MC3:  getCurrentBlockTimestamp
Blocks are batched 100 per JSON-RPC request (node limit) and run concurrently.

Outputs:
    data/states.csv.xz   one row per (block, market), raw integers as on chain
    data/markets.json    market metadata (addresses, version, decimals, leverage)

Usage:
    uv run python fetch_states.py [--stride 150] [--end-block N] [--workers 16]
                                  [--start "2025-10-15 12:02:22"]
"""
from __future__ import annotations

import argparse
import csv
import json
import lzma
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import eth_abi
from tqdm import tqdm

from yb_common import (CHAINLINK_BTCUSD, DATA, MC3, RPC_BATCH, START, block_at, eth_call,
                       fetch_multi_retry, head_block, parse_utc, selector, yb_markets)

MARKET_CALLS = [  # (contract attr, signature, output types, column names, arg attr)
    ("amm", "collateral_amount()", ["uint256"], ["collateral"], None),
    ("amm", "get_debt()", ["uint256"], ["debt"], None),
    ("amm", "value_oracle()", ["(uint256,uint256)"], ["vo_p", "vo_value"], None),
    ("lt", "liquidity()", ["(int256,uint256,uint256,uint256)"],
     ["lt_admin", "lt_total", "lt_ideal_staked", "lt_staked"], None),
    ("lt", "totalSupply()", ["uint256"], ["lt_supply"], None),
    ("lt", "staker()", ["address"], ["lt_staker"], None),
    ("lt", "balanceOf(address)", ["uint256"], ["lt_staked_tokens"], "staker"),
    ("lt", "min_admin_fee()", ["uint256"], ["min_admin_fee"], None),
    ("lt", "pricePerShare()", ["uint256"], ["lt_pps"], None),
    ("amm", "fee()", ["uint256"], ["amm_fee"], None),
]
POOL_CALLS = [
    ("price_scale()", "price_scale"),
    ("price_oracle()", "price_oracle"),
    ("last_prices()", "last_price"),
    ("virtual_price()", "virtual_price"),
    ("xcp_profit()", "xcp_profit"),
    ("totalSupply()", "cp_supply"),
    ("balances(uint256)", "cp_balance0"),
    ("balances(uint256)", "cp_balance1"),
    ("donation_shares()", "cp_donation_shares"),
    ("D()", "cp_D"),
    ("A()", "cp_A"),
    ("gamma()", "cp_gamma"),
    ("mid_fee()", "cp_mid_fee"),
    ("out_fee()", "cp_out_fee"),
    ("fee_gamma()", "cp_fee_gamma"),
]
V3_POOL_CALLS = [("lp_xcp_profit()", "lp_xcp_profit")]

COLUMNS = (["block", "timestamp", "market"]
           + [c for _, _, _, cols, _ in MARKET_CALLS for c in cols]
           + [c for _, c in POOL_CALLS + V3_POOL_CALLS] + ["agg_price", "chainlink_btcusd"])


def build_plan(alive):
    """Calls + decoders for a given set of alive markets (cached per set)."""
    calls, slots = [], []  # slots: (key, out_types, columns)

    def add(target, sig, out, key, cols, args=()):
        types = sig[sig.index("(") + 1:-1].split(",") if args else []
        data = selector(sig) + (eth_abi.encode(types, list(args)) if args else b"")
        calls.append((target, True, data))
        slots.append((key, out, cols))

    for m in alive:
        for attr, sig, out, cols, arg in MARKET_CALLS:
            add(getattr(m, attr), sig, out, ("m", m.idx), cols,
                (getattr(m, arg),) if arg else ())
    for cp in sorted({m.cryptopool for m in alive}):
        v3 = any(m.cryptopool == cp and m.version == "v3" for m in alive)
        for sig, col in POOL_CALLS + (V3_POOL_CALLS if v3 else []):
            args = ({"cp_balance0": 0, "cp_balance1": 1}[col],) if "(uint256)" in sig else ()
            add(cp, sig, ["uint256"], ("cp", cp), [col], args)
    for agg in sorted({m.agg for m in alive}):
        add(agg, "price()", ["uint256"], ("agg", agg), ["agg_price"])
    add(CHAINLINK_BTCUSD, "latestAnswer()", ["int256"], ("ts",), ["chainlink_btcusd"])
    add(MC3, "getCurrentBlockTimestamp()", ["uint256"], ("ts",), ["timestamp"])

    data = selector("aggregate3((address,bool,bytes)[])") + eth_abi.encode(
        ["(address,bool,bytes)[]"], [calls])
    return "0x" + data.hex(), slots


def decode_rows(block, alive, slots, result_hex):
    items = eth_abi.decode(["(bool,bytes)[]"], bytes.fromhex(result_hex[2:]))[0]
    vals = {}
    for (key, out, cols), (ok, ret) in zip(slots, items):
        if not ok or not ret:
            continue
        dec = eth_abi.decode(out, ret)[0]
        dec = dec if isinstance(dec, tuple) else (dec,)
        vals.setdefault(key, {}).update(zip(cols, dec))
    rows = []
    for m in alive:
        r = {"block": block, "market": m.idx, **vals[("ts",)]}
        r.update(vals.get(("m", m.idx), {}))
        r.update(vals.get(("cp", m.cryptopool), {}))
        r.update(vals.get(("agg", m.agg), {}))
        rows.append([r.get(c, "") for c in COLUMNS])
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stride", type=int, default=150)
    ap.add_argument("--end-block", type=int, default=None)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--start", default=START,
                    help=f"window start, UTC (default {START}); its block is always sampled")
    args = ap.parse_args()

    markets = yb_markets()
    with lzma.open(DATA / "events.csv.xz", "rt") as fh:
        flow_blocks = {int(r["block"]) for r in csv.DictReader(fh)
                       if r["contract"] == "amm" or r["event"] == "WithdrawAdminFees"}
    end = args.end_block or max(head_block() - 5, max(flow_blocks))
    start = min(m.created_block for m in markets)

    window_start = block_at(parse_utc(args.start))
    blocks = set(range(start, end + 1, args.stride)) | {end, window_start}
    for b in flow_blocks:
        blocks |= {b - 1, b}
    blocks = sorted(b for b in blocks if start <= b <= end)
    print(f"blocks {start}..{end}: {len(blocks):,} samples "
          f"({len(flow_blocks):,} flow blocks, grid stride {args.stride}, "
          f"window start {args.start} UTC = block {window_start})")

    plans = {}

    def plan_for(block):
        alive = tuple(m for m in markets if m.created_block <= block)
        if alive not in plans:
            plans[alive] = build_plan(alive)
        return alive, plans[alive]

    for b in blocks:  # build all plans up-front (not thread-safe to do lazily)
        plan_for(b)

    def fetch_chunk(chunk):
        payloads, metas = [], []
        for b in chunk:
            alive, (data, slots) = plan_for(b)
            payloads.append(("eth_call", [{"to": MC3, "data": data}, hex(b)]))
            metas.append((b, alive, slots))
        results = fetch_multi_retry(payloads)
        out = []
        for (b, alive, slots), res in zip(metas, results):
            out += decode_rows(b, alive, slots, res)
        return out

    chunks = [blocks[i:i + RPC_BATCH] for i in range(0, len(blocks), RPC_BATCH)]
    DATA.mkdir(exist_ok=True)
    out = DATA / "states.csv.xz"
    n_rows = 0
    with lzma.open(out, "wt", newline="") as fh, \
            ThreadPoolExecutor(args.workers) as ex, \
            tqdm(total=len(blocks), unit="blk", unit_scale=True, desc="states") as pbar:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for chunk, rows in zip(chunks, ex.map(fetch_chunk, chunks)):
            w.writerows(rows)
            n_rows += len(rows)
            pbar.update(len(chunk))

    meta = []
    for m in markets:
        d = asdict(m)
        d["leverage"] = eth_call(m.amm, "LEVERAGE()")
        meta.append(d)
    (DATA / "markets.json").write_text(json.dumps(meta, indent=1))
    print(f"wrote {n_rows:,} rows -> {out}")


if __name__ == "__main__":
    main()
