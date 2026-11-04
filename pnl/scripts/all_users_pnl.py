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

Then queries Gauge.preview_claim(YB, user) at end_block for every
(market, user) pair via Multicall3 — this is the YB that has accrued
to the user but not yet been transferred. It's added to the YB revenue
side of the PnL (valued at end_block YB price), so users that staked
but never claimed aren't penalised.

Each row in the CSV is one (market, user) and reports both PnL views
(redemption-based via preview_withdraw, NAV-based via pricePerShare),
YB rewards (received transfers + pending claimable) valued in BTC, and
position-size summaries. Both unstaked LT and gauge-staked LT count.

HybridVaults: a vault's LT, gauge shares, YB (claimed to the vault, then paid
to its owner; claimable is queried for both) and Deposit / Withdraw events
are its owner's, merged with whatever the owner holds directly
(HybridVaultFactory.vault_to_user). The `hybrid_vault` column names the vault.

Window: PnL is measured from --start (default yb.WINDOW_START,
2025-10-15 12:02:22 UTC; the state at the last block at or before it) to
--end-block. Balances are still rebuilt from each market's full transfer
history, so v1 positions that existed at the start are picked up there and
valued at that block; only intervals after the start count. YB earned in the
window = YB received after the start + claimable at the end - claimable at
the start (gauge claims began at 10:16 UTC that day), the start part valued
at the start's YB and asset prices.

Which view to read (`net_pnl`):
    v1     redemption (preview_withdraw). v1 LT books at the pool's EMA
           oracle, so its pricePerShare drifts far above what a withdrawal
           pays whenever price_scale lags (1.44 vs 0.89 in Feb 2026).
    v2/v3  NAV (pricePerShare). These LT book at price_scale.
Both pure views stay in net_pnl_redem / net_pnl_pps.

--entry-exit (optional): the cost of getting in and out, mostly the pool fee
on depositing into / withdrawing from an imbalanced pool. Positions are marked
at the share price right after each balance change, so what a user paid
above that mark on deposit, or got below it on withdrawal, is otherwise
invisible. From the LT Deposit / Withdraw events (by owner) in the window:
    entry_cost = assets paid - shares x mark at the deposit block
    exit_cost  = shares x mark at the withdrawal block - assets received
with the mark of the row's view (redemption for v1, pricePerShare for v2/v3;
both are in entry_exit_cost_redem / _pps). net_pnl_after_entry_exit =
net_pnl - entry_cost - exit_cost. Same-block round trips (an owner minting
and burning LT in one block: flash-loan bots, whose exit often emits no
Withdraw) are skipped. Events whose owner only routes them (a zap that mints
LT and stakes it in the gauge for the user, or withdraws the user's LT and
sends the user the assets: its own position doesn't move in the transaction)
are passed, pro rata, to the addresses whose position did move that way in the
same transaction, from its receipt (cached in cache/receipts_<markets>.pkl).
Per-market logs show what was skipped, routed and left on the router.

With entry/exit costs the PnL only depends on the marks at the window's ends
(net_pnl_after_entry_exit = value at the end - value at the start - deposited
+ withdrawn + YB, plus LT moved between addresses at its mark then), so it is
the number to use per address: v2's pricePerShare also ran far above
redemption in the Feb 2026 lag (a withdrawal of 10.556 cbBTC marked at 12.52),
which net_pnl alone books as a gain for users who left then.

USD: net_pnl_usd (and entry_exit_cost_usd, net_pnl_after_entry_exit_usd) value
every gain or loss at the asset's crvUSD price (the market's pool
price_oracle) at the time it happened: the PnL between consecutive sampled
blocks (of all markets, so the grid is dense) is priced at the end of that
step; YB at its crvUSD value when received (pending: at its block). It is not
USD PnL in the usual sense: it is the native-asset PnL (vs. holding the
asset) expressed in USD. Per-address totals over all markets go to
pnl_per_address_usd.csv (--out-users), with the as-of block. The summary
also prints the total with all native PnL at end-block prices instead.

Refreshing: with no --end-block the run goes to the finalized head, and the
event / sample caches of the previous run (cache/*_to_<N>.pkl) are extended
from N rather than refetched; superseded cache files are removed.

Usage:
    uv run python scripts/all_users_pnl.py [MARKET_IDX ...] [--entry-exit] [--out FILE]
                                           [--out-users FILE] [--end-block N]
                                           [--start "2025-10-15 12:02:22"]
    # no markets = all of the factory's markets; default outputs:
    # pnl_all_users.csv (per market and address), pnl_per_address_usd.csv
"""
from __future__ import annotations

import os
import pickle
import re
import sys
import time as _time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import polars as pl
import requests
from dotenv import load_dotenv
from eth_abi import decode as abi_decode
from tqdm import tqdm
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from yb import (  # noqa: E402
    EXCLUDED_WALLETS,
    hybrid_vault_owners,
    WINDOW_START,
    all_markets,
    block_at,
    fee_receiver,
    market_deploy_block,
    parse_utc,
    rpc_url,
    version,
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
    {"name": "preview_claim", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "reward", "type": "address"},
                {"name": "user", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
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
# ERC4626-style LT events, identical in v1/v2/v3:
#   Deposit(sender*, owner*, assets, shares), Withdraw(sender*, receiver*, owner*, assets, shares)
DEPOSIT_TOPIC = Web3.keccak(text="Deposit(address,address,uint256,uint256)").to_0x_hex()
WITHDRAW_TOPIC = Web3.keccak(text="Withdraw(address,address,address,uint256,uint256)").to_0x_hex()

YB_TOKEN = Web3.to_checksum_address("0x01791F726B4103694969820be083196cC7c045fF")
YB_POOL = Web3.to_checksum_address("0xec977f46467a3021785cff88894886e617abd65b")
ZERO_ADDR = "0x" + "0" * 40

PROBE_LT = 10**15
# eth_getLogs block range per request. The local node takes 10k; public
# fallbacks often cap at 1000 (set LOGS_CHUNK=1000 there).
CHUNK = int(os.environ.get("LOGS_CHUNK", "10000"))
LOGS_PER_POST = 4    # getLogs requests per JSON-RPC batch
WORKERS = 8          # concurrent HTTP POSTs (getLogs and state sampling)

CACHE_DIR = "cache"
PPS_FLUSH_EVERY = 10_000  # write PPS cache to disk every N blocks

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


def _latest_cache(prefix: str, end_block: int) -> tuple[str, int] | None:
    """(path, N) of the newest cache/{prefix}_to_{N}.pkl with N <= end_block."""
    best = None
    if os.path.isdir(CACHE_DIR):
        for f in os.listdir(CACHE_DIR):
            m = re.fullmatch(re.escape(prefix) + r"_to_(\d+)\.pkl", f)
            if m and int(m.group(1)) <= end_block and (best is None or int(m.group(1)) > best[1]):
                best = (os.path.join(CACHE_DIR, f), int(m.group(1)))
    return best


def _drop_superseded(old_path: str, new_path: str) -> None:
    """Remove a cache file that a newer one (a superset of it) replaces."""
    if old_path != new_path and os.path.exists(old_path):
        os.remove(old_path)
        _log(f"Removed superseded {old_path}")


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


def fetch_logs_chunked(client, address, topics, start_block, end_block, label,
                       with_tx: bool = False):
    """eth_getLogs in CHUNK-block ranges, LOGS_PER_POST per HTTP POST, WORKERS POSTs
    in flight.

    Returns logs in a uniform shape: each log has bytes `topics[i]`, bytes
    `data`, int `blockNumber`, int `logIndex` (and str `tx`, the transaction
    hash, if with_tx) — same as web3.py would return, so _decode_transfer
    doesn't care which transport was used.
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
        out = {
            "topics": [bytes.fromhex(t[2:]) for t in raw["topics"]],
            "data": bytes.fromhex(d[2:]) if d != "0x" else b"",
            "blockNumber": int(raw["blockNumber"], 16),
            "logIndex": int(raw["logIndex"], 16),
        }
        if with_tx:
            out["tx"] = raw["transactionHash"]
        return out

    def _fetch(batch):
        payload = [_payload(j, fb, tb) for j, (fb, tb) in enumerate(batch)]
        resp = _retry(
            lambda: requests.post(url, json=payload, timeout=180),
            label=f"getLogs {batch[0][0]}..{batch[-1][1]}")
        resp.raise_for_status()
        results = resp.json()
        if not isinstance(results, list):
            raise RuntimeError(f"non-list eth_getLogs response: {results}")
        by_id = {r["id"]: r for r in results}
        out = []
        for j in range(len(batch)):
            r = by_id[j]
            if "result" not in r:
                raise RuntimeError(f"eth_getLogs error in chunk "
                                   f"{batch[j][0]}..{batch[j][1]}: {r.get('error')}")
            out += [_normalize(raw_log) for raw_log in r["result"]]
        return out

    logs = []
    t0 = _time.time()
    batches = [chunks[i:i + LOGS_PER_POST] for i in range(0, len(chunks), LOGS_PER_POST)]
    pbar = tqdm(total=len(chunks), desc=label, leave=False, unit="chunk")
    with ThreadPoolExecutor(WORKERS) as ex:
        for batch, part in zip(batches, ex.map(_fetch, batches)):  # in block order
            logs += part
            pbar.update(len(batch))
    pbar.close()
    _log(f"  ← {label}: {len(logs)} logs in {_time.time() - t0:.1f}s")
    return logs


def fetch_tx_transfers(tx_hashes, watch: set[str], path: str) -> dict[str, list]:
    """{tx: [(token, from, to, value)]}: the Transfer logs of `watch` contracts
    (lowercase addresses) in each transaction's receipt. Cached at `path`
    (finalized transactions never change), only new hashes are fetched."""
    got: dict[str, list] = _load_pickle(path) or {}
    need = sorted(set(tx_hashes) - got.keys())
    if not need:
        return got
    url = rpc_url()

    def _fetch(batch):
        payload = [{"jsonrpc": "2.0", "id": j, "method": "eth_getTransactionReceipt",
                    "params": [h]} for j, h in enumerate(batch)]
        resp = _retry(lambda: requests.post(url, json=payload, timeout=180),
                      label=f"receipts {batch[0][:10]}..")
        resp.raise_for_status()
        by_id = {r["id"]: r for r in resp.json()}
        out = {}
        for j, h in enumerate(batch):
            rc = by_id[j].get("result")
            if rc is None:
                raise RuntimeError(f"no receipt for {h}: {by_id[j].get('error')}")
            out[h] = [(lg["address"].lower(), "0x" + lg["topics"][1][-40:],
                       "0x" + lg["topics"][2][-40:], int(lg["data"], 16))
                      for lg in rc["logs"]
                      if lg["address"].lower() in watch and len(lg["topics"]) == 3
                      and lg["topics"][0].lower() == TRANSFER_TOPIC]
        return out

    batches = [need[i:i + 50] for i in range(0, len(need), 50)]
    pbar = tqdm(total=len(need), desc="receipts", unit="tx", leave=False)
    with ThreadPoolExecutor(WORKERS) as ex:
        for part in ex.map(_fetch, batches):
            got.update(part)
            pbar.update(len(part))
    pbar.close()
    _save_pickle(path, got)
    _log(f"  ← {len(need)} receipts; {len(got)} cached in {path}")
    return got


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
    if "-h" in args or "--help" in args:
        print(__doc__)
        sys.exit(0)
    output_csv = "pnl_all_users.csv"
    users_csv = "pnl_per_address_usd.csv"
    end_block_override = None
    start = WINDOW_START
    entry_exit = "--entry-exit" in args
    if entry_exit:
        args.remove("--entry-exit")
    while ("--out" in args or "--out-users" in args or "--end-block" in args
           or "--start" in args):
        if "--out" in args:
            i = args.index("--out")
            output_csv = args[i + 1]
            args = args[:i] + args[i + 2:]
        if "--out-users" in args:
            i = args.index("--out-users")
            users_csv = args[i + 1]
            args = args[:i] + args[i + 2:]
        if "--end-block" in args:
            i = args.index("--end-block")
            end_block_override = int(args[i + 1])
            args = args[:i] + args[i + 2:]
        if "--start" in args:
            i = args.index("--start")
            start = args[i + 1]
            args = args[:i] + args[i + 2:]

    client = w3()
    # Default to the finalized head: caches are extended from where the last
    # run stopped, so they must never hold logs or samples that could reorg
    end_block = end_block_override or client.eth.get_block("finalized")["number"]
    end_ts = client.eth.get_block(end_block)["timestamp"]
    all_mks = {m.idx: m for m in all_markets()}
    market_indices = [int(a) for a in args] or sorted(all_mks)
    window_block = block_at(parse_utc(start))
    print(f"window: from {start} UTC = state at block {window_block}")

    # Per-market context
    ctx_by_idx: dict[int, dict] = {}
    for idx in market_indices:
        m = all_mks[idx]
        token = client.eth.contract(address=m.asset_token, abi=ERC20_ABI)
        sym = token.functions.symbol().call()
        decimals = token.functions.decimals().call()
        deploy = market_deploy_block(idx)
        ctx_by_idx[idx] = {
            "market": m,
            "sym": sym,
            "btc_scale": 10 ** decimals,
            "start_block": deploy,                   # events (balances) from here
            "pnl_from": max(deploy, window_block),   # PnL from here
            "lt_addr": Web3.to_checksum_address(m.lt),
            "gauge_addr": Web3.to_checksum_address(m.staker),
            "cp_addr": Web3.to_checksum_address(m.cryptopool),
        }
        c = ctx_by_idx[idx]
        print(f"Market [{idx}] {version(idx)} {sym}: LT={c['lt_addr']} "
              f"deploy_block={deploy} pnl_from={c['pnl_from']}")
    print(f"end_block: {end_block} "
          f"({datetime.fromtimestamp(end_ts, timezone.utc):%Y-%m-%d %H:%M:%S} UTC)\n")
    tag = "_".join(map(str, market_indices))

    # Event caches are extended, not refetched: the newest cache/<prefix>_to_<N>
    # with N <= end_block is loaded, logs for (N, end_block] are appended, and
    # the result replaces it as <prefix>_to_<end_block>.
    _stage("Stage 1/4: fetching event logs")
    events_path = os.path.join(CACHE_DIR, f"events_{tag}_to_{end_block}.pkl")
    found = _latest_cache(f"events_{tag}", end_block)
    market_events = _load_pickle(found[0]) if found else {}
    have = found[1] if found else 0
    if found:
        _log(f"Loaded cached events to block {have} from {found[0]}")
    if have < end_block:
        for idx, c in ctx_by_idx.items():
            lo = max(c["start_block"], have + 1)
            lt_t = fetch_logs_chunked(
                client, c["lt_addr"], [TRANSFER_TOPIC],
                lo, end_block, f"M{idx} LT.Transfer")
            g_t = fetch_logs_chunked(
                client, c["gauge_addr"], [TRANSFER_TOPIC],
                lo, end_block, f"M{idx} Gauge.Transfer")
            yb_t = fetch_logs_chunked(
                client, YB_TOKEN,
                [TRANSFER_TOPIC, topic_addr(c["market"].staker)],
                lo, end_block, f"M{idx} YB.Transfer (gauge→user)")
            old = market_events.get(idx, ([], [], []))
            market_events[idx] = (old[0] + lt_t, old[1] + g_t, old[2] + yb_t)
        _save_pickle(events_path, market_events)
        _log(f"Saved events to {events_path}")
        if found:
            _drop_superseded(found[0], events_path)

    # Optional: LT Deposit / Withdraw amounts inside the window, for the cost of
    # entering and exiting (mostly pool fees on imbalanced adds / removes)
    market_flows: dict[int, list[tuple[int, str, str, int, int, str]]] = {}
    if entry_exit:
        prefix = f"flows_{tag}_from_{window_block}"
        flows_path = os.path.join(CACHE_DIR, f"{prefix}_to_{end_block}.pkl")
        found = _latest_cache(prefix, end_block)
        market_flows = _load_pickle(found[0]) if found else {}
        if any(len(r) != 6 for recs in market_flows.values() for r in recs[:1]):
            _log(f"{found[0]} predates tx hashes in flows: refetching")
            market_flows = {}
        have = found[1] if found and market_flows else 0
        if found:
            _log(f"Loaded cached Deposit/Withdraw events to block {have} from {found[0]}")
        if have < end_block:
            for idx, c in ctx_by_idx.items():
                logs = fetch_logs_chunked(
                    client, c["lt_addr"], [[DEPOSIT_TOPIC, WITHDRAW_TOPIC]],
                    max(c["pnl_from"], have) + 1, end_block, f"M{idx} LT.Deposit/Withdraw",
                    with_tx=True)
                recs = []
                for lg in logs:
                    is_dep = lg["topics"][0] == bytes.fromhex(DEPOSIT_TOPIC[2:])
                    owner = topic_to_addr(lg["topics"][2 if is_dep else 3].hex())
                    assets = int.from_bytes(lg["data"][:32], "big")
                    shares = int.from_bytes(lg["data"][32:64], "big")
                    recs.append((lg["blockNumber"], "dep" if is_dep else "wd", owner,
                                 assets, shares, lg["tx"]))
                market_flows[idx] = market_flows.get(idx, []) + recs
            _save_pickle(flows_path, market_flows)
            _log(f"Saved Deposit/Withdraw events to {flows_path}")
            if found:
                _drop_superseded(found[0], flows_path)

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

    # HybridVaults hold LT / gauge shares, claim YB and deposit / withdraw for
    # their owner: every address is mapped to its owner before attribution
    seen = {topic_to_addr(lg["topics"][k].hex())
            for lt_t, g_t, _ in market_events.values() for lg in (*lt_t, *g_t)
            for k in (1, 2)}
    vault_owner = hybrid_vault_owners(seen, end_block)
    vaults_of: dict[str, list[str]] = defaultdict(list)
    for v, o in vault_owner.items():
        vaults_of[o].append(v)

    def owner_of(addr: str) -> str:
        return vault_owner.get(addr, addr)

    print(f"Attributing {len(vault_owner)} HybridVault(s) to their owners")
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
            sender, receiver = owner_of(sender), owner_of(receiver)
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
            sender, receiver = owner_of(sender), owner_of(receiver)
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
            receiver = owner_of(receiver)  # a vault claims to itself, then pays its owner
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
        all_blocks.add(c["pnl_from"])
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

    _stage("Stage 2/4: sampling PPS at every event block (Multicall3)")
    # State at a past block never changes, so the newest sample cache is reused
    # whole and only the new blocks are sampled
    pps_path = os.path.join(CACHE_DIR, f"pps_{tag}_to_{end_block}.pkl")
    found_pps = _latest_cache(f"pps_{tag}", end_block)
    cache: dict[int, dict] = _load_pickle(found_pps[0]) if found_pps else {}
    if cache:
        _log(f"Loaded {len(cache)} cached PPS samples from {found_pps[0]}")

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

    def _sample(batch_blocks):
        payload = [
            {"jsonrpc": "2.0", "id": j, "method": "eth_call",
             "params": [{"to": MULTICALL3, "data": agg_calldata}, hex(b)]}
            for j, b in enumerate(batch_blocks)
        ]
        response = _retry(
            lambda: requests.post(url, json=payload, timeout=120),
            label=f"batch@{batch_blocks[0]}..{batch_blocks[-1]}")
        response.raise_for_status()
        results = response.json()
        if not isinstance(results, list):
            raise RuntimeError(f"non-list batch response: {results}")
        by_id = {r["id"]: r for r in results}
        out = []
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
            out.append((b, per_market))
        return out

    pbar = tqdm(total=len(todo), desc="PPS sampling", unit="block")
    since_flush = 0
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    with ThreadPoolExecutor(WORKERS) as ex:
        for part in ex.map(_sample, batches):
            cache.update(part)
            pbar.update(len(part))
            since_flush += len(part)
            if since_flush >= PPS_FLUSH_EVERY:
                _save_pickle(pps_path, cache)
                since_flush = 0
    pbar.close()
    _save_pickle(pps_path, cache)
    if found_pps:
        _drop_superseded(found_pps[0], pps_path)

    # A failed sub-call decodes as 0, which would book a fake -100% / +100% move
    # (and a whole-position fake PnL for anyone whose balance changes at that
    # block). preview_withdraw reverts right after each launch, while the LT is
    # still empty (pricePerShare there ~1.00002). Fill failed samples from the
    # market's deploy on: carry the last good value forward, and back-fill the
    # leading ones with the first good value.
    for idx, c in ctx_by_idx.items():
        live = [b for b in sorted_blocks if b >= c["start_block"]]
        filled = defaultdict(int)
        for k in ("pps", "pw", "cta", "btc"):
            vals = [cache[b][idx][k] for b in live]
            first = next((v for v in vals if v), 0)
            last = first
            for b, v in zip(live, vals):
                if v:
                    last = v
                elif last:
                    cache[b][idx] = {**cache[b][idx], k: last}  # leave the cached copy intact
                    filled[k] += b >= c["pnl_from"]
        filled = {k: n for k, n in filled.items() if n}
        if filled:
            _log(f"  M{idx}: filled failed samples in the window "
                 f"(of {sum(b >= c['pnl_from'] for b in live)} blocks): {filled}")

    _stage("Stage 3/4: pending YB rewards at end_block and window start (Multicall3)")
    # Per-market preview_claim selector — function ABI is identical across
    # gauges, so we encode (YB_TOKEN, user) once per call but reuse the
    # contract's encode_transaction_data path for safety.
    sample_gauge_full = client.eth.contract(
        address=ctx_by_idx[market_indices[0]]["gauge_addr"], abi=GAUGE_ABI)

    def pending_snapshot(at_block: int, markets: list[int], path: str, label: str):
        """{market: {user: claimable YB (atomic)}} at `at_block`, cached at `path`."""
        pending: dict[int, dict[str, int]] = _load_pickle(path) or {}
        if pending:
            _log(f"Loaded cached {label} snapshot from {path}")
        need: list[tuple[int, str]] = []
        for idx in markets:
            pending.setdefault(idx, {})
            need += [(idx, a) for u in market_user_deltas[idx]
                     for a in (u, *vaults_of.get(u, ())) if a not in pending[idx]]
        if not need:
            _log(f"No {label} queries needed (all users cached)")
            return pending
        # Each preview_claim runs an internal _checkpoint, so it's heavier
        # than the simple view calls in stage 2. Cap subcalls per multicall
        # at 50 to keep gas per eth_call modest.
        SUBCALLS_PER_MC = 50
        chunks = [need[i:i + SUBCALLS_PER_MC] for i in range(0, len(need), SUBCALLS_PER_MC)]
        _log(f"Querying preview_claim(YB, user) at block {at_block} for {len(need)} "
             f"(gauge,user) pairs in {len(chunks)} multicall(s)")
        pbar = tqdm(total=len(need), desc=label, unit="user")
        for batch_start in range(0, len(chunks), BATCH_SIZE):
            batch_chunks = chunks[batch_start:batch_start + BATCH_SIZE]
            payloads = []
            for j, chunk in enumerate(batch_chunks):
                calls = []
                for idx, user in chunk:
                    cd = sample_gauge_full.functions.preview_claim(
                        YB_TOKEN, Web3.to_checksum_address(user)
                    )._encode_transaction_data()
                    calls.append((ctx_by_idx[idx]["gauge_addr"], True, cd))
                agg_cd = mc3.functions.aggregate3(calls)._encode_transaction_data()
                payloads.append({
                    "jsonrpc": "2.0", "id": j, "method": "eth_call",
                    "params": [{"to": MULTICALL3, "data": agg_cd}, hex(at_block)],
                })
            response = _retry(
                lambda payloads=payloads: sess.post(url, json=payloads, timeout=180),
                label=f"{label} batch {batch_start}/{len(chunks)}")
            response.raise_for_status()
            results = response.json()
            if not isinstance(results, list):
                raise RuntimeError(f"non-list {label} response: {results}")
            by_id = {r["id"]: r for r in results}
            for j, chunk in enumerate(batch_chunks):
                r = by_id[j]
                if "result" not in r:
                    raise RuntimeError(f"{label} eth_call error: {r.get('error')}")
                decoded = _decode_agg3_return(r["result"])
                for k, (idx, user) in enumerate(chunk):
                    pending[idx][user] = (int.from_bytes(decoded[k][1], "big")
                                          if decoded[k][0] else 0)
                pbar.update(len(chunk))
        pbar.close()
        _save_pickle(path, pending)
        _log(f"Saved {label} snapshot to {path}")
        return pending

    stale = _latest_cache(f"pending_yb_{tag}", end_block - 1)  # claimable changes: requery
    pending_path = os.path.join(CACHE_DIR, f"pending_yb_{tag}_to_{end_block}.pkl")
    pending_yb = pending_snapshot(end_block, market_indices, pending_path, "pending YB (end)")
    if stale:
        _drop_superseded(stale[0], pending_path)
    # Markets already live at the window start: YB that had accrued by then but
    # was claimed later belongs before the window.
    live_at_start = [i for i in market_indices
                     if ctx_by_idx[i]["start_block"] < ctx_by_idx[i]["pnl_from"]]
    pending_start = pending_snapshot(
        window_block, live_at_start,
        os.path.join(CACHE_DIR, f"pending_yb_{tag}_at_{window_block}.pkl"),
        "pending YB (start)") if live_at_start else {}

    for idx in market_indices:
        nz = sum(1 for v in pending_yb[idx].values() if v > 0)
        tot = sum(pending_yb[idx].values()) / 1e18
        tot0 = sum(pending_start.get(idx, {}).values()) / 1e18
        _log(f"  M{idx}: {nz}/{len(pending_yb[idx])} users have pending YB, "
             f"total = {tot:.4f} YB at end, {tot0:.4f} YB at window start")

    # Entry/exit: whose deposit or withdrawal each LT Deposit / Withdraw event is.
    # Skipped: same-block round trips (the owner both mints and burns LT in that
    # block: flash-loan bots, whose exit often emits no Withdraw at all) and
    # excluded owners. An owner whose own LT + gauge position doesn't move in
    # the transaction only routes it (zaps: mint LT, stake it in the gauge for
    # the user; or take the user's unstaked LT, withdraw, send the user the
    # assets). Its event goes to the addresses whose position moved that way in
    # the same transaction, pro rata, from the receipt; if there is none, it
    # stays on the owner.
    market_user_flows: dict[int, dict[str, list]] = {}
    flow_stats: dict[int, dict[str, int]] = {}
    if entry_exit:
        _stage("Entry/exit: attributing LT Deposit / Withdraw events")
        zero = b"\x00" * 32
        prepared = {}
        for idx, c in ctx_by_idx.items():
            pnl_from = c["pnl_from"]
            minted, burned = set(), set()
            for lg in market_events[idx][0]:
                b = lg["blockNumber"]
                if b <= pnl_from:
                    continue
                if lg["topics"][1] == zero:
                    minted.add((owner_of(topic_to_addr(lg["topics"][2].hex())), b))
                if lg["topics"][2] == zero:
                    burned.add((owner_of(topic_to_addr(lg["topics"][1].hex())), b))
            round_trip = minted & burned
            excl = {ZERO_ADDR, c["market"].staker.lower(), c["market"].lt.lower(),
                    fee_dist, *EXCLUDED_WALLETS}
            st = dict.fromkeys(("round_trip", "excluded", "unsampled", "routed",
                                "unresolved"), 0)
            kept = []
            for rec in market_flows.get(idx, []):
                rec = (*rec[:2], owner_of(rec[2]), *rec[3:])
                b, owner = rec[0], rec[2]
                if (owner, b) in round_trip:
                    st["round_trip"] += 1
                elif owner in excl:
                    st["excluded"] += 1
                elif b not in cache:
                    st["unsampled"] += 1
                else:
                    kept.append(rec)
            # Owner's net LT + gauge position change in the event's block (LT units)
            owners = {r[2] for r in kept}
            blk_net: dict[tuple[str, int], float] = defaultdict(float)
            for owner in owners:
                for b, _lidx, dlt, dg in market_user_deltas[idx].get(owner, ()):
                    if b > pnl_from:
                        blk_net[(owner, b)] += dlt + dg * cache[b][idx]["cta"] / PROBE_LT
            routed = [r for r in kept if abs(blk_net[(r[2], r[0])]) < 0.5 * r[4]]
            prepared[idx] = (kept, routed, excl, st)

        watch = {a for c in ctx_by_idx.values() for a in (c["lt_addr"].lower(),
                                                          c["gauge_addr"].lower())}
        tx_logs = fetch_tx_transfers(
            [r[5] for _, routed, _, _ in prepared.values() for r in routed], watch,
            os.path.join(CACHE_DIR, f"receipts_{tag}.pkl"))

        for idx, (kept, routed, excl, st) in prepared.items():
            c = ctx_by_idx[idx]
            lt_a, g_a = c["lt_addr"].lower(), c["gauge_addr"].lower()
            routed_ids = {id(r) for r in routed}
            user_flows: dict[str, list] = defaultdict(list)
            for rec in kept:
                b, kind, owner, assets, shares, tx = rec
                if id(rec) not in routed_ids:
                    user_flows[owner].append((b, kind, assets, shares, 1.0))
                    continue
                g2lt = cache[b][idx]["cta"] / PROBE_LT
                net: dict[str, float] = defaultdict(float)
                for token, frm, to, value in tx_logs[tx]:
                    k = 1.0 if token == lt_a else g2lt if token == g_a else 0.0
                    net[owner_of(frm)] -= value * k
                    net[owner_of(to)] += value * k
                sign = 1 if kind == "dep" else -1
                bens = {a: sign * v for a, v in net.items()
                        if sign * v > 0 and a != owner and a not in excl}
                total = sum(bens.values())
                if abs(net[owner]) >= 0.5 * shares or total <= 0:
                    st["unresolved"] += abs(net[owner]) < 0.5 * shares
                    user_flows[owner].append((b, kind, assets, shares, 1.0))
                    continue
                st["routed"] += 1
                for a, v in bens.items():
                    user_flows[a].append((b, kind, assets, shares, v / total))
            market_user_flows[idx] = user_flows
            flow_stats[idx] = st

    _stage("Stage 4/4: computing per-user PnL")
    rows = []
    for idx, c in ctx_by_idx.items():
        sym = c["sym"]
        btc_scale = c["btc_scale"]
        pps_factor = btc_scale / 10**36
        pnl_from = c["pnl_from"]
        user_deltas = market_user_deltas[idx]
        user_yb = market_user_yb[idx]
        n_idle = 0
        yb_attributed = 0
        v1 = version(idx) == "v1"

        # USD (crvUSD, the pool's price_oracle): each step between consecutive
        # sampled blocks, of any market, is valued at the asset's price at the
        # end of that step, so gains and losses count at the price they
        # happened at. usd_idx[k][b] = USD per share gained since pnl_from, for
        # k in LT / gauge share x redemption / pricePerShare marks.
        usd_idx: dict[str, dict[int, float]] = {k: {} for k in ("lt_r", "g_r", "lt_p", "g_p")}
        acc = dict.fromkeys(usd_idx, 0.0)
        prev = None
        for b in sorted_blocks:
            if b < pnl_from:
                continue
            cm = cache[b][idx]
            r = cm["pw"] / PROBE_LT
            p = cm["pps"] * pps_factor
            cur = {"lt_r": r, "g_r": cm["cta"] * r / PROBE_LT,
                   "lt_p": p, "g_p": cm["cta"] * p / PROBE_LT}
            if prev is not None:
                usd = cm["btc"] / 1e18 / btc_scale  # USD per atomic unit of the asset
                for k in acc:
                    acc[k] += (cur[k] - prev[k]) * usd
            for k in acc:
                usd_idx[k][b] = acc[k]
            prev = cur
        i_lr, i_gr, i_lp, i_gp = (usd_idx[k] for k in ("lt_r", "g_r", "lt_p", "g_p"))
        asset_usd_end = cache[end_block][idx]["btc"] / 1e18

        user_flows = market_user_flows.get(idx, {})
        unattributed = 0.0

        for user, deltas in tqdm(user_deltas.items(),
                                 desc=f"M{idx} {sym}", unit="user", leave=False):
            deltas.sort(key=lambda x: (x[0], x[1]))
            # Balances held at the window start (rebuilt from the full history),
            # then every change after it. A state at block b is the one after b.
            trajectory = []
            lt_run = 0
            g_run = 0
            for b, _lidx, dlt, dg in deltas:
                if b > pnl_from and not trajectory:
                    trajectory.append((pnl_from, lt_run, g_run))
                lt_run += dlt
                g_run += dg
                if b > pnl_from:
                    trajectory.append((b, lt_run, g_run))
            if not trajectory:
                trajectory.append((pnl_from, lt_run, g_run))
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
            weighted_pos = weighted_pos_usd = 0.0
            active_blocks = 0
            usd_r = usd_p = 0.0
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
                usd_r += (lt_held * (i_lr[b_next] - i_lr[b_curr])
                          + g_held * (i_gr[b_next] - i_gr[b_curr]))
                usd_p += (lt_held * (i_lp[b_next] - i_lp[b_curr])
                          + g_held * (i_gp[b_next] - i_gp[b_curr]))
                if positions_redem[j] > 0:
                    weighted_pos += positions_redem[j] * duration
                    weighted_pos_usd += (positions_redem[j] * cc["btc"] / 1e18 / btc_scale
                                         * duration)
                    active_blocks += duration

            max_pos = max(positions_redem)
            avg_pos = weighted_pos / active_blocks if active_blocks else 0

            yb_recv_atomic = 0
            yb_recv_crvusd_atomic = 0.0
            yb_recv_btc_atomic = 0.0
            for b, amount in user_yb.get(user, []):
                if b <= pnl_from:  # claimed before the window
                    continue
                cm = cache[b][idx]
                yb_price = cache[b]["yb"]
                crvusd = amount * yb_price / 10**18
                btc = crvusd * btc_scale / cm["btc"] if cm["btc"] > 0 else 0
                yb_recv_atomic += amount
                yb_recv_crvusd_atomic += crvusd
                yb_recv_btc_atomic += btc

            # Pending (claimable, not yet transferred) YB at end_block,
            # valued at the end_block YB price.
            addrs = (user, *vaults_of.get(user, ()))  # the owner's own + its vault's
            yb_pend_atomic = sum(pending_yb.get(idx, {}).get(a, 0) for a in addrs)
            cm_end = cache[end_block][idx]
            yb_price_end = cache[end_block]["yb"]
            yb_pend_crvusd = yb_pend_atomic * yb_price_end / 10**18
            yb_pend_btc = (yb_pend_crvusd * btc_scale / cm_end["btc"]
                           if cm_end["btc"] > 0 else 0)

            # Claimable at the window start: accrued before it, so it comes off,
            # valued at the start's prices.
            yb_pend0_atomic = sum(pending_start.get(idx, {}).get(a, 0) for a in addrs)
            cm0 = cache[pnl_from][idx]
            yb_pend0_crvusd = yb_pend0_atomic * cache[pnl_from]["yb"] / 10**18
            yb_pend0_btc = (yb_pend0_crvusd * btc_scale / cm0["btc"]
                            if cm0["btc"] > 0 else 0)

            # Entry/exit cost: assets paid vs the shares' value at the deposit
            # block, and shares' value vs assets received at the withdrawal
            # block, both on the marks the PnL views use (positive = paid)
            dep_r = dep_p = wd_r = wd_p = 0.0
            ee_usd_r = ee_usd_p = 0.0
            deposited = withdrawn = 0.0
            for b, kind, assets, shares, w in user_flows.get(user, ()):
                cm = cache[b][idx]
                usd = cm["btc"] / 1e18 / btc_scale
                assets, shares = assets * w, shares * w  # w < 1: a share of a routed event
                v_r = shares * cm["pw"] / PROBE_LT
                v_p = shares * cm["pps"] * pps_factor
                sign = 1 if kind == "dep" else -1  # cost = paid - got
                ee_usd_r += sign * (assets - v_r) * usd
                ee_usd_p += sign * (assets - v_p) * usd
                if kind == "dep":
                    dep_r += assets - v_r
                    dep_p += assets - v_p
                    deposited += assets
                else:
                    wd_r += v_r - assets
                    wd_p += v_p - assets
                    withdrawn += assets

            if max_pos <= 0 and yb_recv_atomic == 0 and yb_pend_atomic == 0:
                n_idle += 1  # nothing held and no YB inside the window
                unattributed += abs(dep_r + wd_r if v1 else dep_p + wd_p)
                continue
            yb_attributed += yb_recv_atomic

            yb_total_atomic = yb_recv_atomic + yb_pend_atomic - yb_pend0_atomic
            yb_crvusd_atomic = yb_recv_crvusd_atomic + yb_pend_crvusd - yb_pend0_crvusd
            yb_btc_atomic = yb_recv_btc_atomic + yb_pend_btc - yb_pend0_btc

            pnl_lt_redem_btc = pnl_lt_redem / btc_scale
            pnl_g_redem_btc = pnl_g_redem / btc_scale
            pnl_lt_pps_btc = pnl_lt_pps / btc_scale
            pnl_g_pps_btc = pnl_g_pps / btc_scale
            yb_btc = yb_btc_atomic / btc_scale
            net_redem = pnl_lt_redem_btc + pnl_g_redem_btc + yb_btc
            net_pps = pnl_lt_pps_btc + pnl_g_pps_btc + yb_btc

            row = {
                "market": idx,
                "version": version(idx),
                "symbol": sym,
                "user": user,
                "hybrid_vault": " ".join(vaults_of.get(user, ())) or None,
                "max_pos": max_pos / btc_scale,
                "avg_pos": avg_pos / btc_scale,
                "pnl_lt_redem": pnl_lt_redem_btc,
                "pnl_gauge_redem": pnl_g_redem_btc,
                "pnl_lt_pps": pnl_lt_pps_btc,
                "pnl_gauge_pps": pnl_g_pps_btc,
                "yb_received": yb_recv_atomic / 1e18,
                "yb_pending": yb_pend_atomic / 1e18,
                "yb_pending_start": yb_pend0_atomic / 1e18,
                "yb_earned": yb_total_atomic / 1e18,
                "yb_value_crvusd": yb_crvusd_atomic / 1e18,
                "yb_value_in_asset": yb_btc,
                "net_pnl_redem": net_redem,
                "net_pnl_pps": net_pps,
                "net_pnl": net_redem if v1 else net_pps,
                "asset_usd_end": asset_usd_end,
                "avg_pos_usd": weighted_pos_usd / active_blocks if active_blocks else 0,
                "net_pnl_usd": (usd_r if v1 else usd_p) + yb_crvusd_atomic / 1e18,
            }
            if entry_exit:
                ee_r = (dep_r + wd_r) / btc_scale
                ee_p = (dep_p + wd_p) / btc_scale
                ee_usd = ee_usd_r if v1 else ee_usd_p
                row.update({
                    "deposited": deposited / btc_scale,
                    "withdrawn": withdrawn / btc_scale,
                    "entry_cost": (dep_r if v1 else dep_p) / btc_scale,
                    "exit_cost": (wd_r if v1 else wd_p) / btc_scale,
                    "entry_exit_cost_redem": ee_r,
                    "entry_exit_cost_pps": ee_p,
                    "entry_exit_cost": ee_r if v1 else ee_p,
                    "net_pnl_after_entry_exit": row["net_pnl"] - (ee_r if v1 else ee_p),
                    "entry_exit_cost_usd": ee_usd,
                    "net_pnl_after_entry_exit_usd": row["net_pnl_usd"] - ee_usd,
                })
            rows.append(row)

        # YB that left the gauge in the window to an address that never held
        # this market's LT/gauge shares is not attributed to anyone
        yb_out = sum(a for recs in user_yb.values() for b, a in recs if b > pnl_from)
        _log(f"  M{idx}: {n_idle} users idle in the window; YB claimed in window "
             f"{yb_out / 1e18:,.0f}, attributed {yb_attributed / 1e18:,.0f}")
        if entry_exit:
            st = flow_stats[idx]
            _log(f"  M{idx}: Deposit/Withdraw events skipped: {st['round_trip']} same-block "
                 f"round trips, {st['excluded']} by excluded owners, {st['unsampled']} "
                 f"unsampled; {st['routed']} routed ones passed to the users behind them, "
                 f"{st['unresolved']} left on the router; |cost| on idle owners "
                 f"(unattributed) {unattributed / btc_scale:,.4f} {sym}")

    df = pl.DataFrame(rows).sort(["market", "max_pos"], descending=[False, True])
    df.write_csv(output_csv)
    _stage(f"Done — wrote {len(df)} rows to {output_csv}")

    print(f"\nMarket totals (window from block {window_block}; net_pnl = redemption "
          f"view for v1, pricePerShare view for v2/v3):")
    summary = (df.group_by(["market", "version", "symbol"])
                 .agg([
                     pl.col("user").n_unique().alias("n_users"),
                     pl.col("max_pos").sum().alias("Σ max_pos"),
                     pl.col("net_pnl").sum().alias("Σ net_pnl"),
                     pl.col("net_pnl_redem").sum().alias("Σ net_pnl_redem"),
                     pl.col("net_pnl_pps").sum().alias("Σ net_pnl_pps"),
                     pl.col("yb_earned").sum().alias("Σ yb_earned"),
                     pl.col("yb_pending_start").sum().alias("Σ yb_pend_start"),
                     pl.col("yb_value_in_asset").sum().alias("Σ yb_in_asset"),
                     *([pl.col("entry_cost").sum().alias("Σ entry_cost"),
                        pl.col("exit_cost").sum().alias("Σ exit_cost"),
                        pl.col("net_pnl_after_entry_exit").sum().alias("Σ net_after_ee")]
                       if entry_exit else []),
                 ])
                 .sort("market"))
    pl.Config.set_tbl_rows(20)
    pl.Config.set_tbl_cols(20)
    pl.Config.set_tbl_width_chars(250)
    print(summary)

    # One row per address, all markets summed in USD
    usd_cols = ["net_pnl_usd",
                *(["net_pnl_after_entry_exit_usd", "entry_exit_cost_usd"] if entry_exit else []),
                "position_pnl_usd", "yb_usd", "avg_pos_usd"]
    per = (df.with_columns(
               (pl.col("net_pnl_usd") - pl.col("yb_value_crvusd")).alias("position_pnl_usd"),
               pl.col("yb_value_crvusd").alias("yb_usd"))
             .group_by("user")
             .agg(*[pl.col(k).sum() for k in usd_cols],
                  pl.col("market").sort().cast(pl.String).str.join(" ").alias("markets"),
                  pl.col("version").unique().sort().str.join(" ").alias("versions"),
                  pl.col("hybrid_vault").first())
             .with_columns(*[pl.when(pl.col(k).round(2) == 0).then(0.0)  # no "-0.0"
                               .otherwise(pl.col(k).round(2)).alias(k) for k in usd_cols],
                           pl.lit(end_block).alias("as_of_block"),
                           pl.lit(f"{datetime.fromtimestamp(end_ts, timezone.utc):%Y-%m-%d %H:%M:%S}")
                             .alias("as_of_utc"))
             .select("user", *usd_cols, "markets", "versions", "hybrid_vault",
                     "as_of_block", "as_of_utc")
             .sort("user"))
    per.write_csv(users_csv)
    final = "net_pnl_after_entry_exit_usd" if entry_exit else "net_pnl_usd"
    at_end = (df["net_pnl_after_entry_exit" if entry_exit else "net_pnl"]
              * df["asset_usd_end"]).sum()
    print(f"\nWrote {len(per)} addresses to {users_csv} (USD = crvUSD at the pool oracle; "
          f"as of block {end_block})")
    print(f"  Σ {final}: ${per[final].sum():,.0f}  "
          f"({(per[final] > 0).sum()} in profit, {(per[final] < 0).sum()} at a loss)")
    print(f"  Σ net_pnl_usd ${per['net_pnl_usd'].sum():,.0f} = positions "
          f"${per['position_pnl_usd'].sum():,.0f} + YB ${per['yb_usd'].sum():,.0f}")
    print(f"  (native PnL all at end-block prices instead: ${at_end:,.0f})")
    if not entry_exit:
        print("  Without --entry-exit: net_pnl_usd misses what users paid getting in and "
              "out, and for v2/v3 users who left while pricePerShare ran above "
              "redemption (Feb 2026) it overstates their PnL.")


if __name__ == "__main__":
    main()
