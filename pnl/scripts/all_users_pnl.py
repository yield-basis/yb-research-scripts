"""Per-user BTC/ETH PnL spreadsheet across multiple YB markets.

Pulls all LT.Transfer + Gauge.Transfer events (no user filter) per market,
plus YB.Transfer events from each gauge. Derives per-user balance
trajectories from the event deltas, then samples five metrics per market
at each unique event block — all markets in a SINGLE Multicall3 per block:

  per market:
    - LT.pricePerShare           (NAV per share, slippage-free)
    - LT.preview_withdraw(P)     (redemption value at marginal probe)
    - Gauge.convertToAssets(P)   (LT-equivalent per gauge share)
    - market.cryptopool.price_oracle()  (asset price in crvUSD)
  shared:
    - YB_POOL.price_oracle()             (YB price in crvUSD)

Each row in the CSV is one (market, user) and reports both PnL views
(redemption-based via preview_withdraw, NAV-based via pricePerShare),
YB rewards valued at receipt blocks, and position-size summaries.

Usage:
    uv run python scripts/all_users_pnl.py MARKET_IDX [MARKET_IDX ...] [--out FILE]
    # default output: pnl_all_users.csv
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
    EXCLUDED_WALLETS,
    all_markets,
    fee_receiver,
    market_deploy_block,
    rpc_url,
    w3,
)

load_dotenv()


MULTICALL3 = Web3.to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")
MULTICALL3_ABI = [{
    "name": "aggregate3", "type": "function", "stateMutability": "payable",
    "inputs": [{
        "name": "calls", "type": "tuple[]",
        "components": [
            {"name": "target", "type": "address"},
            {"name": "allowFailure", "type": "bool"},
            {"name": "callData", "type": "bytes"},
        ],
    }],
    "outputs": [{
        "name": "returnData", "type": "tuple[]",
        "components": [
            {"name": "success", "type": "bool"},
            {"name": "returnData", "type": "bytes"},
        ],
    }],
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

TRANSFER_TOPIC = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex()

YB_TOKEN = Web3.to_checksum_address("0x01791F726B4103694969820be083196cC7c045fF")
YB_POOL = Web3.to_checksum_address("0xec977f46467a3021785cff88894886e617abd65b")
ZERO_ADDR = "0x" + "0" * 40

PROBE_LT = 10**15
CHUNK = 1000

CACHE_DIR = "cache"
PPS_FLUSH_EVERY = 500  # write PPS cache to disk every N blocks

# JSON-RPC batch size for eth_call sampling. Bench (scripts/bench_rpc.py)
# showed 20 is the sweet spot on the local node — node caps batches at
# ~100-150 and shows scheduling pathologies at certain mid sizes.
BATCH_SIZE = 100


def _log(msg: str) -> None:
    # tqdm.write so progress-bar lines don't get clobbered.
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
    """Run fn() with exponential backoff on transient errors."""
    last_err = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:  # connection reset, http errors, server overload, etc.
            last_err = e
            wait = min(2 ** attempt, 30)
            _log(f"    {label}: {type(e).__name__} attempt {attempt + 1}/{retries}, "
                 f"sleeping {wait}s")
            _time.sleep(wait)
    raise last_err  # type: ignore[misc]


def topic_addr(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def topic_to_addr(topic_hex: str) -> str:
    h = topic_hex if topic_hex.startswith("0x") else "0x" + topic_hex
    return "0x" + h[-40:]


def fetch_logs_chunked(client, address, topics, start_block, end_block, label):
    """eth_getLogs in 1000-block chunks, BATCH_SIZE chunks per HTTP POST.

    Returns logs in a uniform shape: each log has bytes `topics[i]`, bytes
    `data`, int `blockNumber`, int `logIndex` — same as web3.py would
    return, so _decode_transfer doesn't care which transport was used.
    """
    # Pre-build the (from, to) windows.
    chunks = []
    block = start_block
    while block <= end_block:
        to = min(block + CHUNK - 1, end_block)
        chunks.append((block, to))
        block = to + 1

    topics_for_rpc = [
        ("0x" + t.hex() if isinstance(t, bytes) else t) for t in topics
    ]
    addr = Web3.to_checksum_address(address)
    url = rpc_url()
    sess = requests.Session()

    def _payload(req_id, from_b, to_b):
        return {
            "jsonrpc": "2.0", "id": req_id, "method": "eth_getLogs",
            "params": [{
                "fromBlock": hex(from_b),
                "toBlock": hex(to_b),
                "address": addr,
                "topics": topics_for_rpc,
            }],
        }

    def _normalize(raw):
        d = raw["data"]
        return {
            "topics": [bytes.fromhex(t[2:]) for t in raw["topics"]],
            "data": bytes.fromhex(d[2:]) if d != "0x" else b"",
            "blockNumber": int(raw["blockNumber"], 16),
            "logIndex": int(raw["logIndex"], 16),
        }

    logs = []
    t0 = _time.time()
    pbar = tqdm(total=len(chunks), desc=label, leave=False, unit="chunk")
    for batch_start in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[batch_start:batch_start + BATCH_SIZE]
        payload = [_payload(j, fb, tb) for j, (fb, tb) in enumerate(batch)]
        resp = _retry(
            lambda payload=payload: sess.post(url, json=payload, timeout=180),
            label=f"getLogs {batch[0][0]}..{batch[-1][1]}")
        resp.raise_for_status()
        results = resp.json()
        if not isinstance(results, list):
            raise RuntimeError(f"non-list eth_getLogs response: {results}")
        by_id = {r["id"]: r for r in results}
        for j in range(len(batch)):
            r = by_id[j]
            if "result" not in r:
                raise RuntimeError(f"eth_getLogs error in chunk "
                                   f"{batch[j][0]}..{batch[j][1]}: {r.get('error')}")
            for raw_log in r["result"]:
                logs.append(_normalize(raw_log))
        pbar.update(len(batch))
    pbar.close()
    _log(f"  ← {label}: {len(logs)} logs in {_time.time() - t0:.1f}s")
    return logs


def _decode_transfer(lg):
    sender = topic_to_addr(lg["topics"][1].hex())
    receiver = topic_to_addr(lg["topics"][2].hex())
    data = lg["data"]
    if isinstance(data, bytes):
        value = int.from_bytes(data, "big")
    else:
        value = int(data, 16)
    return lg["blockNumber"], lg["logIndex"], sender, receiver, value


def main() -> None:
    args = sys.argv[1:]
    output_csv = "pnl_all_users.csv"
    end_block_override = None
    while "--out" in args or "--end-block" in args:
        if "--out" in args:
            i = args.index("--out")
            output_csv = args[i + 1]
            args = args[:i] + args[i + 2:]
        if "--end-block" in args:
            i = args.index("--end-block")
            end_block_override = int(args[i + 1])
            args = args[:i] + args[i + 2:]
    market_indices = [int(a) for a in args]
    if not market_indices:
        print(__doc__)
        sys.exit(1)

    client = w3()
    end_block = end_block_override if end_block_override else client.eth.block_number
    all_mks = {m.idx: m for m in all_markets()}

    # Per-market context
    ctx_by_idx: dict[int, dict] = {}
    for idx in market_indices:
        m = all_mks[idx]
        token = client.eth.contract(address=m.asset_token, abi=ERC20_ABI)
        sym = token.functions.symbol().call()
        decimals = token.functions.decimals().call()
        ctx_by_idx[idx] = {
            "market": m,
            "sym": sym,
            "btc_scale": 10 ** decimals,
            "start_block": market_deploy_block(idx),
            "lt_addr": Web3.to_checksum_address(m.lt),
            "gauge_addr": Web3.to_checksum_address(m.staker),
            "cp_addr": Web3.to_checksum_address(m.cryptopool),
        }
        c = ctx_by_idx[idx]
        print(f"Market [{idx}] {sym}: LT={c['lt_addr']} "
              f"deploy_block={c['start_block']}")
    print(f"end_block: {end_block}\n")

    _stage("Stage 1/3: fetching event logs")
    events_path = os.path.join(
        CACHE_DIR, f"events_{'_'.join(map(str, market_indices))}_to_{end_block}.pkl")
    market_events = _load_pickle(events_path)
    if market_events is not None:
        _log(f"Loaded cached events from {events_path}")
    else:
        market_events = {}
        for idx, c in ctx_by_idx.items():
            lt_t = fetch_logs_chunked(
                client, c["lt_addr"], [TRANSFER_TOPIC],
                c["start_block"], end_block, f"M{idx} LT.Transfer")
            g_t = fetch_logs_chunked(
                client, c["gauge_addr"], [TRANSFER_TOPIC],
                c["start_block"], end_block, f"M{idx} Gauge.Transfer")
            yb_t = fetch_logs_chunked(
                client, YB_TOKEN,
                [TRANSFER_TOPIC, topic_addr(c["market"].staker)],
                c["start_block"], end_block, f"M{idx} YB.Transfer (gauge→user)")
            market_events[idx] = (lt_t, g_t, yb_t)
        _save_pickle(events_path, market_events)
        _log(f"Saved events to {events_path}")

    # Per-market user_deltas / user_yb. Excluded addresses (NOT real users):
    #   - 0x0                mint/burn pseudo-address
    #   - staker             gauge contract holds LT on behalf of stakers
    #   - lt                 defensive: LT contract itself
    #   - fee_receiver       FeeDistributor — protocol contract that holds
    #                        LT pending distribution to veYB holders
    #   - EXCLUDED_WALLETS   contracts where deposited LT and any YB
    #                        rewards are unrescuable (e.g. a Uniswap v4
    #                        hook lacking any token-rescue function)
    fee_dist = fee_receiver().lower()
    print(f"\nExcluding fee_receiver (FeeDistributor) = {fee_dist}")
    print(f"Excluding {len(EXCLUDED_WALLETS)} hard-coded wallet(s):")
    for a in EXCLUDED_WALLETS:
        print(f"  {a}")
    print()
    market_user_deltas: dict[int, dict[str, list]] = {}
    market_user_yb: dict[int, dict[str, list]] = {}
    for idx, c in ctx_by_idx.items():
        EXCLUDE = {
            ZERO_ADDR,
            c["market"].staker.lower(),
            c["market"].lt.lower(),
            fee_dist,
            *EXCLUDED_WALLETS,
        }
        user_deltas: dict[str, list] = defaultdict(list)
        n_skipped = 0
        lt_t, g_t, yb_t = market_events[idx]
        for lg in lt_t:
            b, lidx, sender, receiver, value = _decode_transfer(lg)
            if sender not in EXCLUDE:
                user_deltas[sender].append((b, lidx, -value, 0))
            else:
                n_skipped += 1
            if receiver not in EXCLUDE:
                user_deltas[receiver].append((b, lidx, value, 0))
            else:
                n_skipped += 1
        for lg in g_t:
            b, lidx, sender, receiver, value = _decode_transfer(lg)
            if sender not in EXCLUDE:
                user_deltas[sender].append((b, lidx, 0, -value))
            else:
                n_skipped += 1
            if receiver not in EXCLUDE:
                user_deltas[receiver].append((b, lidx, 0, value))
            else:
                n_skipped += 1
        market_user_deltas[idx] = user_deltas

        user_yb: dict[str, list] = defaultdict(list)
        for lg in yb_t:
            b, _lidx, _sender, receiver, value = _decode_transfer(lg)
            if receiver in EXCLUDE:
                continue
            user_yb[receiver].append((b, value))
        market_user_yb[idx] = user_yb

        # Sanity check: gauge address should never appear as a user.
        gauge_lc = c["market"].staker.lower()
        assert gauge_lc not in user_deltas, f"gauge {gauge_lc} leaked into M{idx} users"
        _log(f"  M{idx}: skipped {n_skipped} transfers involving "
             f"{len(EXCLUDE)} excluded addresses (gauge / LT / 0x0)")

        print(f"  M{idx}: {len(user_deltas)} users / {len(user_yb)} YB receipt-users")

    # Union of all blocks across all markets
    all_blocks: set[int] = {end_block}
    for idx, c in ctx_by_idx.items():
        all_blocks.add(c["start_block"])
        for deltas in market_user_deltas[idx].values():
            all_blocks.update(b for b, _, _, _ in deltas)
        for yb_rec in market_user_yb[idx].values():
            all_blocks.update(b for b, _ in yb_rec)
    sorted_blocks = sorted(all_blocks)
    print(f"\n{len(sorted_blocks)} unique sample blocks across all markets\n")

    mc3 = client.eth.contract(address=MULTICALL3, abi=MULTICALL3_ABI)

    # Build the merged per-block call list. 4 calls per market + 1 shared.
    sample_lt = client.eth.contract(address=ctx_by_idx[market_indices[0]]["lt_addr"], abi=LT_ABI)
    sample_gauge = client.eth.contract(address=ctx_by_idx[market_indices[0]]["gauge_addr"], abi=GAUGE_ABI)
    sample_pool = client.eth.contract(address=ctx_by_idx[market_indices[0]]["cp_addr"], abi=POOL_ABI)
    pps_cd = sample_lt.functions.pricePerShare()._encode_transaction_data()
    pw_cd = sample_lt.functions.preview_withdraw(PROBE_LT)._encode_transaction_data()
    cta_cd = sample_gauge.functions.convertToAssets(PROBE_LT)._encode_transaction_data()
    pool_cd = sample_pool.functions.price_oracle()._encode_transaction_data()

    base_calls = []
    market_offsets: dict[int, int] = {}
    for idx, c in ctx_by_idx.items():
        market_offsets[idx] = len(base_calls)
        base_calls.extend([
            (c["lt_addr"], True, pps_cd),     # +0 pricePerShare
            (c["lt_addr"], True, pw_cd),      # +1 preview_withdraw
            (c["gauge_addr"], True, cta_cd),  # +2 convertToAssets
            (c["cp_addr"], True, pool_cd),    # +3 cryptopool.price_oracle
        ])
    yb_offset = len(base_calls)
    base_calls.append((YB_POOL, True, pool_cd))  # YB price (shared)

    _stage("Stage 2/3: sampling PPS at every event block (Multicall3)")
    pps_path = os.path.join(
        CACHE_DIR, f"pps_{'_'.join(map(str, market_indices))}_to_{end_block}.pkl")
    cache: dict[int, dict] = _load_pickle(pps_path) or {}
    if cache:
        _log(f"Loaded {len(cache)} cached PPS samples from {pps_path}")

    todo = [b for b in sorted_blocks if b not in cache]
    _log(f"{len(base_calls)} metrics/block × {len(todo)} blocks remaining "
         f"({len(sorted_blocks) - len(todo)} cached); batch_size={BATCH_SIZE}")

    # Encode the aggregate3 calldata once — same for every block, only the
    # block_identifier varies. Then send N eth_calls per HTTP POST as a
    # JSON-RPC batch.
    agg_calldata = mc3.functions.aggregate3(base_calls)._encode_transaction_data()
    url = rpc_url()
    sess = requests.Session()

    def _decode_agg3_return(hex_str: str) -> list[tuple[bool, bytes]]:
        ret_bytes = bytes.fromhex(hex_str[2:] if hex_str.startswith("0x") else hex_str)
        return abi_decode(["(bool,bytes)[]"], ret_bytes)[0]

    pbar = tqdm(total=len(todo), desc="PPS sampling", unit="block")
    n_processed = 0
    for batch_start in range(0, len(todo), BATCH_SIZE):
        batch_blocks = todo[batch_start:batch_start + BATCH_SIZE]
        payload = [
            {"jsonrpc": "2.0", "id": j, "method": "eth_call",
             "params": [{"to": MULTICALL3, "data": agg_calldata}, hex(b)]}
            for j, b in enumerate(batch_blocks)
        ]
        response = _retry(
            lambda payload=payload: sess.post(url, json=payload, timeout=120),
            label=f"batch@{batch_blocks[0]}..{batch_blocks[-1]}")
        response.raise_for_status()
        results = response.json()
        if not isinstance(results, list):
            raise RuntimeError(f"non-list batch response: {results}")
        by_id = {r["id"]: r for r in results}
        for j, b in enumerate(batch_blocks):
            r = by_id[j]
            if "result" not in r:
                raise RuntimeError(f"eth_call error at block {b}: {r.get('error')}")
            decoded = _decode_agg3_return(r["result"])
            per_market = {}
            for idx in market_indices:
                o = market_offsets[idx]
                per_market[idx] = {
                    "pps": int.from_bytes(decoded[o + 0][1], "big") if decoded[o + 0][0] else 0,
                    "pw":  int.from_bytes(decoded[o + 1][1], "big") if decoded[o + 1][0] else 0,
                    "cta": int.from_bytes(decoded[o + 2][1], "big") if decoded[o + 2][0] else 0,
                    "btc": int.from_bytes(decoded[o + 3][1], "big") if decoded[o + 3][0] else 0,
                }
            per_market["yb"] = int.from_bytes(decoded[yb_offset][1], "big") if decoded[yb_offset][0] else 0
            cache[b] = per_market
            n_processed += 1
            pbar.update(1)
            if n_processed % PPS_FLUSH_EVERY == 0:
                _save_pickle(pps_path, cache)
    pbar.close()
    _save_pickle(pps_path, cache)

    _stage("Stage 3/3: computing per-user PnL")
    rows = []
    for idx, c in ctx_by_idx.items():
        sym = c["sym"]
        btc_scale = c["btc_scale"]
        pps_factor = btc_scale / 10**36
        start_block = c["start_block"]
        user_deltas = market_user_deltas[idx]
        user_yb = market_user_yb[idx]

        for user, deltas in tqdm(user_deltas.items(),
                                 desc=f"M{idx} {sym}", unit="user", leave=False):
            deltas.sort(key=lambda x: (x[0], x[1]))
            trajectory = [(start_block, 0, 0)]
            lt_run = 0
            g_run = 0
            for b, _lidx, dlt, dg in deltas:
                lt_run += dlt
                g_run += dg
                trajectory.append((b, lt_run, g_run))
            trajectory.append((end_block, lt_run, g_run))

            positions_redem = []
            for b, lt_bal, g_bal in trajectory:
                cm = cache[b][idx]
                lt_pps_r = cm["pw"] / PROBE_LT
                g_pps_r = cm["cta"] * lt_pps_r / PROBE_LT
                positions_redem.append(lt_bal * lt_pps_r + g_bal * g_pps_r)

            pnl_lt_redem = pnl_g_redem = 0.0
            pnl_lt_pps = pnl_g_pps = 0.0
            # avg_pos is the time-average position while ACTIVELY HOLDING
            # (intervals with position == 0 are excluded), so it reads in
            # BTC of typical position size, not "BTC across the full window".
            weighted_pos = 0.0
            active_blocks = 0
            for j in range(len(trajectory) - 1):
                b_curr = trajectory[j][0]
                b_next = trajectory[j + 1][0]
                duration = b_next - b_curr
                if duration <= 0:
                    continue
                lt_held = trajectory[j][1]
                g_held = trajectory[j][2]
                cc = cache[b_curr][idx]
                cn = cache[b_next][idx]
                lt_pps_r_c = cc["pw"] / PROBE_LT
                lt_pps_r_n = cn["pw"] / PROBE_LT
                g_pps_r_c = cc["cta"] * lt_pps_r_c / PROBE_LT
                g_pps_r_n = cn["cta"] * lt_pps_r_n / PROBE_LT
                pnl_lt_redem += lt_held * (lt_pps_r_n - lt_pps_r_c)
                pnl_g_redem += g_held * (g_pps_r_n - g_pps_r_c)
                lt_pps_p_c = cc["pps"] * pps_factor
                lt_pps_p_n = cn["pps"] * pps_factor
                g_pps_p_c = cc["cta"] * lt_pps_p_c / PROBE_LT
                g_pps_p_n = cn["cta"] * lt_pps_p_n / PROBE_LT
                pnl_lt_pps += lt_held * (lt_pps_p_n - lt_pps_p_c)
                pnl_g_pps += g_held * (g_pps_p_n - g_pps_p_c)
                if positions_redem[j] > 0:
                    weighted_pos += positions_redem[j] * duration
                    active_blocks += duration

            max_pos = max(positions_redem)
            avg_pos = weighted_pos / active_blocks if active_blocks else 0

            yb_total_atomic = 0
            yb_crvusd_atomic = 0.0
            yb_btc_atomic = 0.0
            for b, amount in user_yb.get(user, []):
                cm = cache[b][idx]
                yb_price = cache[b]["yb"]
                crvusd = amount * yb_price / 10**18
                btc = crvusd * btc_scale / cm["btc"] if cm["btc"] > 0 else 0
                yb_total_atomic += amount
                yb_crvusd_atomic += crvusd
                yb_btc_atomic += btc

            pnl_lt_redem_btc = pnl_lt_redem / btc_scale
            pnl_g_redem_btc = pnl_g_redem / btc_scale
            pnl_lt_pps_btc = pnl_lt_pps / btc_scale
            pnl_g_pps_btc = pnl_g_pps / btc_scale
            yb_btc = yb_btc_atomic / btc_scale

            rows.append({
                "market": idx,
                "symbol": sym,
                "user": user,
                "max_pos": max_pos / btc_scale,
                "avg_pos": avg_pos / btc_scale,
                "pnl_lt_redem": pnl_lt_redem_btc,
                "pnl_gauge_redem": pnl_g_redem_btc,
                "pnl_lt_pps": pnl_lt_pps_btc,
                "pnl_gauge_pps": pnl_g_pps_btc,
                "yb_received": yb_total_atomic / 1e18,
                "yb_value_crvusd": yb_crvusd_atomic / 1e18,
                "yb_value_in_asset": yb_btc,
                "net_pnl_redem": pnl_lt_redem_btc + pnl_g_redem_btc + yb_btc,
                "net_pnl_pps": pnl_lt_pps_btc + pnl_g_pps_btc + yb_btc,
            })

    df = pl.DataFrame(rows).sort(["market", "max_pos"], descending=[False, True])
    df.write_csv(output_csv)
    _stage(f"Done — wrote {len(df)} rows to {output_csv}")

    print("\nMarket totals:")
    summary = (df.group_by(["market", "symbol"])
                 .agg([
                     pl.col("user").n_unique().alias("n_users"),
                     pl.col("max_pos").sum().alias("Σ max_pos"),
                     pl.col("net_pnl_redem").sum().alias("Σ net_pnl_redem"),
                     pl.col("net_pnl_pps").sum().alias("Σ net_pnl_pps"),
                     pl.col("yb_value_in_asset").sum().alias("Σ yb_in_asset"),
                 ])
                 .sort("market"))
    pl.Config.set_tbl_rows(20)
    print(summary)


if __name__ == "__main__":
    main()
