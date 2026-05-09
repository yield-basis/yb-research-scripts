"""Per-user BTC×blocks integral across markets 3-6 from AIRDROP_1_BLOCK to now.

For every (user, market) row, integrates the user's BTC-equivalent position
over time (block count) from AIRDROP_1_BLOCK forward, then sums across the
four markets to give a single per-user value in `btc_blocks`. The
normalized column adds to 1.0 across all rows.

Reuses the events + PPS caches written by all_users_pnl.py — no fresh
RPC fetches except a single Multicall3 at AIRDROP_1_BLOCK if it's not
already in the PPS cache (then it gets persisted back).

Excludes the same non-user addresses as all_users_pnl.py (gauge, LT,
fee_receiver, ZERO_ADDR, EXCLUDED_WALLETS).

Usage:
    uv run python scripts/btc_time_integral.py [--end-block N] [--out FILE]
"""
from __future__ import annotations

import os
import pickle
import sys
import time as _time
from collections import defaultdict

import polars as pl
import requests
from dotenv import load_dotenv
from eth_abi import decode as abi_decode
from tqdm import tqdm
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from yb import (  # noqa: E402
    AIRDROP_1_BLOCK,
    EXCLUDED_WALLETS,
    all_markets,
    fee_receiver,
    rpc_url,
    w3,
)

load_dotenv()


MULTICALL3 = Web3.to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")
MULTICALL3_ABI = [{
    "name": "aggregate3", "type": "function", "stateMutability": "payable",
    "inputs": [{"name": "calls", "type": "tuple[]", "components": [
        {"name": "target", "type": "address"},
        {"name": "allowFailure", "type": "bool"},
        {"name": "callData", "type": "bytes"},
    ]}],
    "outputs": [{"name": "returnData", "type": "tuple[]", "components": [
        {"name": "success", "type": "bool"},
        {"name": "returnData", "type": "bytes"},
    ]}],
}]
LT_ABI = [
    {"name": "pricePerShare", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "preview_withdraw", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokens", "type": "uint256"}], "outputs": [{"type": "uint256"}]},
]
GAUGE_ABI = [
    {"name": "convertToAssets", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "uint256"}], "outputs": [{"type": "uint256"}]},
]
ERC20_ABI = [
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "string"}]},
]
POOL_ABI = [{"name": "price_oracle", "type": "function", "stateMutability": "view",
             "inputs": [], "outputs": [{"type": "uint256"}]}]

ZERO_ADDR = "0x" + "0" * 40
PROBE_LT = 10**15
CACHE_DIR = "cache"
MARKET_INDICES = (3, 4, 5, 6)


def _log(msg: str) -> None:
    tqdm.write(f"[{_time.strftime('%H:%M:%S')}] {msg}")


def _stage(title: str) -> None:
    print(flush=True)
    bar = "═" * (len(title) + 4)
    tqdm.write(bar)
    tqdm.write(f"  {title}")
    tqdm.write(bar)


def _load_pickle(path):
    if os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f)
    return None


def _save_pickle(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(data, f)
    os.replace(tmp, path)


def _retry(fn, label: str, retries: int = 8):
    last = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            last = e
            wait = min(2 ** attempt, 30)
            _log(f"  retry {label}: {type(e).__name__} attempt {attempt + 1}/{retries}, sleep {wait}s")
            _time.sleep(wait)
    raise last  # type: ignore[misc]


def topic_to_addr(topic_hex: str) -> str:
    h = topic_hex if topic_hex.startswith("0x") else "0x" + topic_hex
    return "0x" + h[-40:]


def _decode_transfer(lg):
    t1 = lg["topics"][1]
    t2 = lg["topics"][2]
    sender = topic_to_addr(t1.hex() if isinstance(t1, bytes) else t1)
    receiver = topic_to_addr(t2.hex() if isinstance(t2, bytes) else t2)
    data = lg["data"]
    value = int.from_bytes(data, "big") if isinstance(data, bytes) else int(data, 16)
    blk = lg["blockNumber"] if isinstance(lg["blockNumber"], int) else int(lg["blockNumber"], 16)
    log_idx = lg["logIndex"] if isinstance(lg["logIndex"], int) else int(lg["logIndex"], 16)
    return blk, log_idx, sender, receiver, value


def main() -> None:
    args = sys.argv[1:]
    end_block_override = None
    output_csv = "btc_time_integral.csv"
    while "--end-block" in args or "--out" in args:
        if "--end-block" in args:
            i = args.index("--end-block")
            end_block_override = int(args[i + 1])
            args = args[:i] + args[i + 2:]
        if "--out" in args:
            i = args.index("--out")
            output_csv = args[i + 1]
            args = args[:i] + args[i + 2:]

    client = w3()
    end_block = end_block_override or client.eth.block_number
    print(f"Window: [{AIRDROP_1_BLOCK}, {end_block}]  ({end_block - AIRDROP_1_BLOCK:,} blocks)")

    _stage("Stage 1/4: market context (current prices for reference only)")
    by_idx = {m.idx: m for m in all_markets()}
    ctx: dict[int, dict] = {}
    snapshot_prices: dict[int, float] = {}
    for idx in MARKET_INDICES:
        m = by_idx[idx]
        token = client.eth.contract(address=m.asset_token, abi=ERC20_ABI)
        sym = token.functions.symbol().call()
        decimals = token.functions.decimals().call()
        cp = client.eth.contract(address=m.cryptopool, abi=POOL_ABI)
        asset_in_crvusd = cp.functions.price_oracle().call() / 1e18
        snapshot_prices[idx] = asset_in_crvusd
        ctx[idx] = {
            "market": m, "sym": sym, "btc_scale": 10 ** decimals,
            "lt_addr": Web3.to_checksum_address(m.lt),
            "gauge_addr": Web3.to_checksum_address(m.staker),
            "cp_addr": Web3.to_checksum_address(m.cryptopool),
        }
    btc_now = snapshot_prices[3]
    for idx in MARKET_INDICES:
        p = snapshot_prices[idx]
        _log(f"M{idx} {ctx[idx]['sym']:<5}  asset_in_crvusd=${p:>10,.2f}  "
             f"asset_per_btc(now)={p / btc_now:.6f}")
    _log("Integration uses per-block asset/BTC ratio from cached cryptopool "
         "price_oracle samples — current prices above are info only.")

    _stage("Stage 2/4: load caches from all_users_pnl.py")
    suffix = f"{'_'.join(map(str, MARKET_INDICES))}_to_{end_block}"
    events_path = os.path.join(CACHE_DIR, f"events_{suffix}.pkl")
    pps_path = os.path.join(CACHE_DIR, f"pps_{suffix}.pkl")
    market_events = _load_pickle(events_path)
    pps_cache = _load_pickle(pps_path)
    if market_events is None or pps_cache is None:
        raise SystemExit(
            f"\nMissing cache files {events_path} / {pps_path}. "
            "Run all_users_pnl.py first."
        )
    _log(f"events for {len(market_events)} markets, "
         f"{len(pps_cache)} PPS sample blocks loaded")

    _stage("Stage 3/4: build user_deltas, exclude non-users")
    fee_dist = fee_receiver().lower()
    _log(f"fee_receiver = {fee_dist}")
    _log(f"EXCLUDED_WALLETS = {EXCLUDED_WALLETS}")

    market_user_deltas: dict[int, dict[str, list]] = {}
    total_decoded = 0
    for idx, c in ctx.items():
        EXCLUDE = {
            ZERO_ADDR,
            c["market"].staker.lower(),
            c["market"].lt.lower(),
            fee_dist,
            *EXCLUDED_WALLETS,
        }
        user_deltas: dict[str, list] = defaultdict(list)
        lt_t, g_t, _yb_t = market_events[idx]
        for lg in tqdm(lt_t, desc=f"M{idx} LT.Transfer", leave=False, unit="ev"):
            b, lidx, sender, receiver, value = _decode_transfer(lg)
            if sender not in EXCLUDE:
                user_deltas[sender].append((b, lidx, -value, 0))
            if receiver not in EXCLUDE:
                user_deltas[receiver].append((b, lidx, value, 0))
        for lg in tqdm(g_t, desc=f"M{idx} Gauge.Transfer", leave=False, unit="ev"):
            b, lidx, sender, receiver, value = _decode_transfer(lg)
            if sender not in EXCLUDE:
                user_deltas[sender].append((b, lidx, 0, -value))
            if receiver not in EXCLUDE:
                user_deltas[receiver].append((b, lidx, 0, value))
        market_user_deltas[idx] = user_deltas
        total_decoded += len(lt_t) + len(g_t)
        _log(f"M{idx} {c['sym']}: {len(user_deltas)} users / "
             f"{len(lt_t)} LT.Transfer + {len(g_t)} Gauge.Transfer")
    _log(f"decoded {total_decoded} events total")

    if AIRDROP_1_BLOCK not in pps_cache:
        _log(f"AIRDROP_1_BLOCK not in cache; sampling via Multicall3...")
        mc3 = client.eth.contract(address=MULTICALL3, abi=MULTICALL3_ABI)
        sample_lt = client.eth.contract(address=ctx[3]["lt_addr"], abi=LT_ABI)
        sample_g = client.eth.contract(address=ctx[3]["gauge_addr"], abi=GAUGE_ABI)
        sample_pool = client.eth.contract(address=ctx[3]["cp_addr"], abi=POOL_ABI)
        pps_cd = sample_lt.functions.pricePerShare()._encode_transaction_data()
        pw_cd = sample_lt.functions.preview_withdraw(PROBE_LT)._encode_transaction_data()
        cta_cd = sample_g.functions.convertToAssets(PROBE_LT)._encode_transaction_data()
        pool_cd = sample_pool.functions.price_oracle()._encode_transaction_data()
        calls = []
        for idx in MARKET_INDICES:
            calls.extend([
                (ctx[idx]["lt_addr"], True, pps_cd),
                (ctx[idx]["lt_addr"], True, pw_cd),
                (ctx[idx]["gauge_addr"], True, cta_cd),
                (ctx[idx]["cp_addr"], True, pool_cd),
            ])
        rets = _retry(
            lambda: mc3.functions.aggregate3(calls).call(block_identifier=AIRDROP_1_BLOCK),
            label="multicall@airdrop")
        per_market = {}
        for j, idx in enumerate(MARKET_INDICES):
            base = j * 4
            per_market[idx] = {
                "pps": int.from_bytes(rets[base + 0][1], "big") if rets[base + 0][0] else 0,
                "pw":  int.from_bytes(rets[base + 1][1], "big") if rets[base + 1][0] else 0,
                "cta": int.from_bytes(rets[base + 2][1], "big") if rets[base + 2][0] else 0,
                "btc": int.from_bytes(rets[base + 3][1], "big") if rets[base + 3][0] else 0,
            }
        per_market["yb"] = 0  # not needed here, keep schema consistent
        pps_cache[AIRDROP_1_BLOCK] = per_market
        _save_pickle(pps_path, pps_cache)
        _log(f"saved augmented pps cache → {pps_path}")

    _stage("Stage 4/4: integrate BTC × blocks per user (per-block prices)")
    integrals: dict[str, float] = defaultdict(float)
    skipped_no_btc_ref = 0
    for idx, c in ctx.items():
        btc_scale = c["btc_scale"]
        users = market_user_deltas[idx]
        for user, deltas in tqdm(users.items(), desc=f"M{idx} {c['sym']}",
                                  leave=False, unit="user"):
            deltas.sort(key=lambda x: (x[0], x[1]))

            # Replay events up to AIRDROP_1_BLOCK to get the anchor balance.
            lt_run, g_run = 0, 0
            split_idx = 0
            for i, (b, _li, dlt, dg) in enumerate(deltas):
                if b < AIRDROP_1_BLOCK:
                    lt_run += dlt
                    g_run += dg
                    split_idx = i + 1
                else:
                    break

            trajectory: list[tuple[int, int, int]] = [(AIRDROP_1_BLOCK, lt_run, g_run)]
            for b, _li, dlt, dg in deltas[split_idx:]:
                lt_run += dlt
                g_run += dg
                trajectory.append((b, lt_run, g_run))
            trajectory.append((end_block, lt_run, g_run))

            for j in range(len(trajectory) - 1):
                b_curr, lt_bal, g_bal = trajectory[j]
                b_next = trajectory[j + 1][0]
                duration = b_next - b_curr
                if duration <= 0 or (lt_bal == 0 and g_bal == 0):
                    continue
                block_data = pps_cache[b_curr]
                cm = block_data[idx]
                btc_ref = block_data[3]            # WBTC market = our BTC reference
                if cm["pw"] == 0 or btc_ref["btc"] == 0 or cm["btc"] == 0:
                    skipped_no_btc_ref += 1
                    continue
                lt_pps_atomic = cm["pw"] / PROBE_LT
                g_pps_atomic = cm["cta"] * lt_pps_atomic / PROBE_LT
                pos_atomic = lt_bal * lt_pps_atomic + g_bal * g_pps_atomic
                # Per-block asset/BTC conversion via cached cryptopool oracles.
                asset_per_btc_block = cm["btc"] / btc_ref["btc"]
                pos_btc = (pos_atomic / btc_scale) * asset_per_btc_block
                integrals[user] += pos_btc * duration
    if skipped_no_btc_ref:
        _log(f"skipped {skipped_no_btc_ref} intervals with missing PPS / BTC ref")

    rows = sorted(integrals.items(), key=lambda kv: -kv[1])
    total = sum(v for _, v in rows)
    _log(f"users with non-zero exposure: {len(rows)}")
    _log(f"Σ btc_blocks = {total:.4f}")

    df = pl.DataFrame({
        "user": [u for u, _ in rows],
        "btc_blocks": [v for _, v in rows],
        "fraction": [v / total if total else 0.0 for _, v in rows],
    })
    df.write_csv(output_csv)
    _stage(f"Done — wrote {len(df)} rows to {output_csv}")
    print(f"Σ fraction = {df['fraction'].sum():.6f}  (should be 1.0)")
    pl.Config.set_fmt_str_lengths(50)
    print("\nTop 20 by btc_blocks:")
    print(df.head(20))


if __name__ == "__main__":
    main()
