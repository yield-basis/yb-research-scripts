"""Per-user BTC PnL debug — pricePerShare variant (no slippage).

Same shape as scripts/debug_user.py, but values LT positions via
LT.pricePerShare() — the contract's own non-manipulable NAV per share
(see LT.vy:746). pricePerShare returns
  v.total * 10**18 // v.supply_tokens
where v.total is the AMM oracle value re-priced into 1e18-normalized
BTC. Compare side-by-side with debug_user.py to see how marginal-
withdrawal slippage shows up versus fundamental NAV.

Usage:
    uv run python scripts/debug_user_pps.py 0xUSER MARKET_IDX [N_BLOCKS]
"""
from __future__ import annotations

import os
import sys
import time as _time

import polars as pl
from dotenv import load_dotenv
from eth_abi import decode as abi_decode
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from yb import all_markets, market_deploy_block, w3  # noqa: E402

load_dotenv()


MULTICALL3 = Web3.to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")
MULTICALL3_ABI = [{
    "name": "aggregate3",
    "type": "function",
    "stateMutability": "payable",
    "inputs": [{
        "name": "calls",
        "type": "tuple[]",
        "components": [
            {"name": "target", "type": "address"},
            {"name": "allowFailure", "type": "bool"},
            {"name": "callData", "type": "bytes"},
        ],
    }],
    "outputs": [{
        "name": "returnData",
        "type": "tuple[]",
        "components": [
            {"name": "success", "type": "bool"},
            {"name": "returnData", "type": "bytes"},
        ],
    }],
}]

LT_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "pricePerShare", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint256"}]},
]
GAUGE_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "convertToAssets", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "uint256"}], "outputs": [{"type": "uint256"}]},
]
ERC20_ABI = [
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "string"}]},
]

DEPOSIT_TOPIC = "0x" + Web3.keccak(text="Deposit(address,address,uint256,uint256)").hex()
WITHDRAW_TOPIC = "0x" + Web3.keccak(text="Withdraw(address,address,address,uint256,uint256)").hex()
TRANSFER_TOPIC = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex()

# YB token + the YB/crvUSD twocrypto pool used to price rewards.
# price_oracle() returns YB price in crvUSD (1e18 fixed point), with crvUSD ≈ $1.
YB_TOKEN = Web3.to_checksum_address("0x01791F726B4103694969820be083196cC7c045fF")
YB_POOL = Web3.to_checksum_address("0xec977f46467a3021785cff88894886e617abd65b")

POOL_ABI = [{"name": "price_oracle", "type": "function", "stateMutability": "view",
             "inputs": [], "outputs": [{"type": "uint256"}]}]

PROBE_LT = 10**15
CHUNK = 1000


def _log(msg: str) -> None:
    print(f"[{_time.strftime('%H:%M:%S')}] {msg}", flush=True)


def topic_addr(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def fetch_logs_chunked(client, address, topics, start, end, label):
    """Direct eth_getLogs in CHUNK-sized windows. Sequential — local node
    overloads under any concurrency."""
    t0 = _time.time()
    _log(f"  fetching {label} ({(end - start + 1) // CHUNK + 1} chunks)...")
    logs = []
    block = start
    while block <= end:
        to = min(block + CHUNK - 1, end)
        params = {
            "fromBlock": block,
            "toBlock": to,
            "address": Web3.to_checksum_address(address),
            "topics": topics,
        }
        logs.extend(client.eth.get_logs(params))
        block = to + 1
    _log(f"  ← {label}: {len(logs)} logs in {_time.time() - t0:.1f}s")
    return logs


def topics_with_user(topic0, user_pos, user_topic):
    """Build a topics array for eth_getLogs. user_pos in {1,2,3}."""
    topics: list[str | None] = [topic0, None, None, None]
    topics[user_pos] = user_topic
    while topics and topics[-1] is None:
        topics.pop()
    return topics


def decode_log(log, indexed_count, value_types):
    """Decode an eth_getLogs entry. indexed_count = number of indexed args
    (after topic0). value_types = ABI types of NON-indexed args."""
    indexed = [log["topics"][i + 1].hex() for i in range(indexed_count)]
    data = log["data"]
    if isinstance(data, bytes):
        data_bytes = data
    else:
        data_bytes = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    decoded = abi_decode(value_types, data_bytes) if value_types else ()
    return {
        "block": log["blockNumber"],
        "log_index": log["logIndex"],
        "indexed": indexed,
        "values": list(decoded),
    }


def topic_to_addr(topic_hex):
    """Strip a 32-byte topic to a 20-byte address."""
    h = topic_hex if topic_hex.startswith("0x") else "0x" + topic_hex
    return "0x" + h[-40:]


def main() -> None:
    user = sys.argv[1].lower()
    market_idx = int(sys.argv[2])

    client = w3()
    end_block = client.eth.block_number

    market = next(m for m in all_markets() if m.idx == market_idx)
    if len(sys.argv) > 3:
        start_block = end_block - int(sys.argv[3])
    else:
        start_block = market_deploy_block(market_idx)

    token = client.eth.contract(address=market.asset_token, abi=ERC20_ABI)
    sym = token.functions.symbol().call()
    decimals = token.functions.decimals().call()
    btc_scale = 10 ** decimals

    print(f"User:   {user}")
    print(f"Market: [{market_idx}] {sym}")
    print(f"  LT:    {market.lt}")
    print(f"  Gauge: {market.staker}")
    print(f"  decimals: {decimals}")
    print(f"  block range: {start_block}..{end_block} ({end_block - start_block:,} blocks)")
    print()

    user_topic = topic_addr(user)
    lt_addr = Web3.to_checksum_address(market.lt)
    gauge_addr = Web3.to_checksum_address(market.staker)

    deps_in = fetch_logs_chunked(
        client, lt_addr, topics_with_user(DEPOSIT_TOPIC, 2, user_topic),
        start_block, end_block, "LT.Deposit (owner=user)")
    wds_owner = fetch_logs_chunked(
        client, lt_addr, topics_with_user(WITHDRAW_TOPIC, 3, user_topic),
        start_block, end_block, "LT.Withdraw (owner=user)")
    lt_t_from = fetch_logs_chunked(
        client, lt_addr, topics_with_user(TRANSFER_TOPIC, 1, user_topic),
        start_block, end_block, "LT.Transfer (sender=user)")
    lt_t_to = fetch_logs_chunked(
        client, lt_addr, topics_with_user(TRANSFER_TOPIC, 2, user_topic),
        start_block, end_block, "LT.Transfer (receiver=user)")
    g_t_from = fetch_logs_chunked(
        client, gauge_addr, topics_with_user(TRANSFER_TOPIC, 1, user_topic),
        start_block, end_block, "Gauge.Transfer (sender=user)")
    g_t_to = fetch_logs_chunked(
        client, gauge_addr, topics_with_user(TRANSFER_TOPIC, 2, user_topic),
        start_block, end_block, "Gauge.Transfer (receiver=user)")

    # Decode events into a uniform delta stream.
    # LT.Deposit(sender indexed, owner indexed, assets, shares) →
    #   indexed: [sender, owner], values: [assets, shares]
    # LT.Withdraw(sender, receiver, owner indexed, assets, shares) →
    #   indexed: [sender, receiver, owner], values: [assets, shares]
    # Transfer(from indexed, to indexed, value) → indexed: [from, to], values: [value]
    rows = []
    deltas = []  # (block, log_idx, lt_delta_atomic, g_delta_atomic)

    for lg in deps_in:
        d = decode_log(lg, indexed_count=2, value_types=["uint256", "uint256"])
        assets, shares = d["values"]
        rows.append({"block": d["block"], "log": d["log_index"], "kind": "LT.Deposit",
                     "btc_in": assets / btc_scale, "lt_delta": shares / 1e18,
                     "counter": "(mint)"})
        deltas.append((d["block"], d["log_index"], shares, 0))

    for lg in wds_owner:
        d = decode_log(lg, indexed_count=3, value_types=["uint256", "uint256"])
        assets, shares = d["values"]
        rows.append({"block": d["block"], "log": d["log_index"], "kind": "LT.Withdraw",
                     "btc_in": -assets / btc_scale, "lt_delta": -shares / 1e18,
                     "counter": topic_to_addr(d["indexed"][1])})
        deltas.append((d["block"], d["log_index"], -shares, 0))

    for lg in lt_t_from:
        d = decode_log(lg, indexed_count=2, value_types=["uint256"])
        sender, receiver = d["indexed"]
        if int(sender, 16) == 0:
            continue  # mint = Deposit, already handled
        value = d["values"][0]
        rows.append({"block": d["block"], "log": d["log_index"], "kind": "LT.Transfer (sent)",
                     "btc_in": 0.0, "lt_delta": -value / 1e18,
                     "counter": topic_to_addr(receiver)})
        deltas.append((d["block"], d["log_index"], -value, 0))

    for lg in lt_t_to:
        d = decode_log(lg, indexed_count=2, value_types=["uint256"])
        sender, _ = d["indexed"]
        if int(sender, 16) == 0:
            continue
        value = d["values"][0]
        rows.append({"block": d["block"], "log": d["log_index"], "kind": "LT.Transfer (recv)",
                     "btc_in": 0.0, "lt_delta": value / 1e18,
                     "counter": topic_to_addr(sender)})
        deltas.append((d["block"], d["log_index"], value, 0))

    for lg in g_t_from:
        d = decode_log(lg, indexed_count=2, value_types=["uint256"])
        sender, receiver = d["indexed"]
        if int(sender, 16) == 0:
            continue
        value = d["values"][0]
        rows.append({"block": d["block"], "log": d["log_index"], "kind": "Gauge.Transfer (sent)",
                     "btc_in": 0.0, "lt_delta": 0.0,
                     "counter": topic_to_addr(receiver),
                     "gauge_delta": -value / 1e18})
        deltas.append((d["block"], d["log_index"], 0, -value))

    for lg in g_t_to:
        d = decode_log(lg, indexed_count=2, value_types=["uint256"])
        sender, _ = d["indexed"]
        # Gauge mint to user (sender=0x0) IS a balance change to track —
        # there's no Deposit event on the gauge for this user.
        value = d["values"][0]
        rows.append({"block": d["block"], "log": d["log_index"], "kind": "Gauge.Transfer (recv)",
                     "btc_in": 0.0, "lt_delta": 0.0,
                     "counter": topic_to_addr(sender),
                     "gauge_delta": value / 1e18})
        deltas.append((d["block"], d["log_index"], 0, value))

    if not rows:
        print("No activity for this user in this market.")
        return

    df = pl.DataFrame(rows).sort(["block", "log"])
    pl.Config.set_tbl_rows(500)
    pl.Config.set_fmt_str_lengths(60)
    print("\nChronological event timeline:")
    print(df)

    btc_deposited = sum(r["btc_in"] for r in rows if r["kind"] == "LT.Deposit")
    btc_withdrawn = sum(-r["btc_in"] for r in rows if r["kind"] == "LT.Withdraw")
    print()
    print(f"BTC deposited (LT.Deposit assets):  {btc_deposited:.8f}")
    print(f"BTC withdrawn (LT.Withdraw assets): {btc_withdrawn:.8f}")

    # --- Multicall3-based current-state and PPS sampling ---
    lt = client.eth.contract(address=lt_addr, abi=LT_ABI)
    gauge = client.eth.contract(address=gauge_addr, abi=GAUGE_ABI)
    mc3 = client.eth.contract(address=MULTICALL3, abi=MULTICALL3_ABI)
    user_addr = client.to_checksum_address(user)

    pps_calldata = lt.functions.pricePerShare()._encode_transaction_data()
    cta_calldata = gauge.functions.convertToAssets(PROBE_LT)._encode_transaction_data()
    bal_lt_calldata = lt.functions.balanceOf(user_addr)._encode_transaction_data()
    bal_g_calldata = gauge.functions.balanceOf(user_addr)._encode_transaction_data()

    def multicall_at(block, calls):
        """calls: list of (address, calldata). Returns list of (success, bytes).
        Uses aggregate3 with allowFailure=True so e.g. pricePerShare on a
        fresh LT (zero supply path returns 1e18, but other reverts may occur)
        don't crash the whole sampling pass."""
        agg_calls = [(addr, True, data) for addr, data in calls]
        results = mc3.functions.aggregate3(agg_calls).call(block_identifier=block)
        return results

    # Starting balances at start_block (zero if contracts didn't exist).
    _log("Querying starting balances at start_block via Multicall3...")
    if client.eth.get_code(lt_addr, block_identifier=start_block) == b"":
        lt_start, g_start = 0, 0
    else:
        rets = multicall_at(start_block, [
            (lt_addr, bal_lt_calldata),
            (gauge_addr, bal_g_calldata),
        ])
        lt_start = int.from_bytes(rets[0][1], "big") if rets[0][0] else 0
        g_start = int.from_bytes(rets[1][1], "big") if rets[1][0] else 0
    print(f"Starting balances at block {start_block}: "
          f"LT={lt_start / 1e18:.6f}, Gauge={g_start / 1e18:.6f}")
    if lt_start != 0 or g_start != 0:
        print("  ⚠ non-zero starting balance — start_block is too late, "
              "user had pre-existing positions we won't see in events.")

    deltas.sort(key=lambda e: (e[0], e[1]))
    trajectory = [(start_block, lt_start, g_start)]
    lt_run = lt_start
    g_run = g_start
    for block, _, dlt, dg in deltas:
        lt_run += dlt
        g_run += dg
        trajectory.append((block, lt_run, g_run))
    trajectory.append((end_block, lt_run, g_run))

    # Sanity-check final balances.
    rets = multicall_at(end_block, [
        (lt_addr, bal_lt_calldata),
        (gauge_addr, bal_g_calldata),
    ])
    cur_lt_atomic = int.from_bytes(rets[0][1], "big") if rets[0][0] else 0
    cur_g_atomic = int.from_bytes(rets[1][1], "big") if rets[1][0] else 0
    print(f"\nPredicted final balances:  LT={lt_run / 1e18:.6f}, "
          f"Gauge={g_run / 1e18:.6f}")
    print(f"On-chain final balances:   LT={cur_lt_atomic / 1e18:.6f}, "
          f"Gauge={cur_g_atomic / 1e18:.6f}")

    # Sample PPS at each unique trajectory block.
    # pricePerShare returns 1e18-normalized BTC × 1e18 per LT atomic, so
    # to convert to "BTC atomic per LT atomic" we multiply by btc_scale / 1e36.
    unique_blocks = sorted({b for b, _, _ in trajectory})
    _log(f"Sampling pricePerShare at {len(unique_blocks)} blocks via Multicall3...")
    pps_cache: dict[int, tuple[int, int]] = {}
    t0 = _time.time()
    for i, b in enumerate(unique_blocks):
        rets = multicall_at(b, [
            (lt_addr, pps_calldata),
            (gauge_addr, cta_calldata),
        ])
        pps_raw = int.from_bytes(rets[0][1], "big") if rets[0][0] else 0
        cta = int.from_bytes(rets[1][1], "big") if rets[1][0] else 0
        pps_cache[b] = (pps_raw, cta)
        if (i + 1) % 10 == 0 or i == len(unique_blocks) - 1:
            _log(f"  PPS {i + 1}/{len(unique_blocks)} ({_time.time() - t0:.1f}s)")

    # Integrate PnL. Convert pricePerShare (1e36-fixed normalized-BTC per LT)
    # to a float "BTC atomic per LT atomic" rate.
    pps_factor = btc_scale / 10**36

    pnl_lt_atomic = 0.0
    pnl_gauge_atomic = 0.0
    for i in range(len(trajectory) - 1):
        b_curr, lt_held, g_held = trajectory[i]
        b_next = trajectory[i + 1][0]
        if b_next == b_curr:
            continue
        pps_curr, cta_curr = pps_cache[b_curr]
        pps_next, cta_next = pps_cache[b_next]
        lt_pps_curr = pps_curr * pps_factor          # BTC atomic per LT atomic
        lt_pps_next = pps_next * pps_factor
        # gauge share → LT via convertToAssets, then LT → BTC via pricePerShare
        g_pps_curr = cta_curr * lt_pps_curr / PROBE_LT
        g_pps_next = cta_next * lt_pps_next / PROBE_LT
        pnl_lt_atomic += lt_held * (lt_pps_next - lt_pps_curr)
        pnl_gauge_atomic += g_held * (g_pps_next - g_pps_curr)

    pnl_lt_btc = pnl_lt_atomic / btc_scale
    pnl_gauge_btc = pnl_gauge_atomic / btc_scale

    # Position-size summary: BTC value at each trajectory block, and a
    # time-weighted (block-weighted) average across the full period.
    positions = []
    for b, lt_bal, g_bal in trajectory:
        pps_raw, cta = pps_cache[b]
        lt_pps = pps_raw * pps_factor
        g_pps = cta * lt_pps / PROBE_LT
        pos = lt_bal * lt_pps + g_bal * g_pps
        positions.append(pos)
    max_pos_atomic = max(positions)
    weighted_sum = 0.0
    active_blocks = 0
    for i in range(len(trajectory) - 1):
        b_curr = trajectory[i][0]
        b_next = trajectory[i + 1][0]
        duration = b_next - b_curr
        if duration <= 0 or positions[i] <= 0:
            continue
        weighted_sum += positions[i] * duration
        active_blocks += duration
    avg_pos_atomic = weighted_sum / active_blocks if active_blocks else 0

    print()
    print(f"Max position size:        {max_pos_atomic / btc_scale:.8f} {sym}")
    print(f"Avg position size (time): {avg_pos_atomic / btc_scale:.8f} {sym}")
    print(f"PnL on LT positions:      {pnl_lt_btc:+.8f} {sym}")
    print(f"PnL on gauge positions:   {pnl_gauge_btc:+.8f} {sym}")

    # ---- YB rewards earned by this user (gauge stakers receive YB) ----
    #
    # Filter strictly to YB.Transfer(sender=gauge, receiver=user) — anyone
    # can send YB, so without the gauge-sender filter we'd lump in DEX
    # trades, OTC transfers, etc.
    # Each receipt is priced at YB_POOL.price_oracle() (crvUSD per YB, 1e18)
    # and converted to BTC via the market's cryptopool price_oracle.
    gauge_topic = topic_addr(market.staker)
    yb_t_to = fetch_logs_chunked(
        client, YB_TOKEN,
        [TRANSFER_TOPIC, gauge_topic, user_topic],
        start_block, end_block, "YB.Transfer (gauge → user)")

    yb_value_crvusd_atomic = 0
    yb_value_btc_atomic = 0
    yb_total_atomic = 0

    if yb_t_to:
        yb_pool = client.eth.contract(address=YB_POOL, abi=POOL_ABI)
        cryptopool = client.eth.contract(
            address=Web3.to_checksum_address(market.cryptopool), abi=POOL_ABI)
        yb_price_cd = yb_pool.functions.price_oracle()._encode_transaction_data()
        btc_price_cd = cryptopool.functions.price_oracle()._encode_transaction_data()

        _log(f"Pricing {len(yb_t_to)} YB receipts via Multicall3...")
        for lg in yb_t_to:
            d = decode_log(lg, indexed_count=2, value_types=["uint256"])
            block = d["block"]
            amount = d["values"][0]
            rets = multicall_at(block, [
                (YB_POOL, yb_price_cd),
                (Web3.to_checksum_address(market.cryptopool), btc_price_cd),
            ])
            yb_price = int.from_bytes(rets[0][1], "big") if rets[0][0] else 0
            btc_price = int.from_bytes(rets[1][1], "big") if rets[1][0] else 0
            # YB and crvUSD both 18 decimals.
            crvusd_value_atomic = amount * yb_price // 10**18
            # btc_price is "crvUSD per BTC" in 1e18 fixed point (twocrypto
            # normalizes both tokens to 18 internally regardless of token decimals).
            # btc_atomic_in_native_scale = (crvusd_value / btc_price) * btc_scale.
            if btc_price > 0:
                btc_value_atomic = crvusd_value_atomic * btc_scale // btc_price
            else:
                btc_value_atomic = 0
            yb_total_atomic += amount
            yb_value_crvusd_atomic += crvusd_value_atomic
            yb_value_btc_atomic += btc_value_atomic

    yb_total = yb_total_atomic / 1e18
    yb_value_crvusd = yb_value_crvusd_atomic / 1e18
    yb_value_btc = yb_value_btc_atomic / btc_scale

    print()
    print(f"YB received (across {len(yb_t_to)} receipts): {yb_total:.4f} YB")
    print(f"YB rewards value at receipt:")
    print(f"  in crvUSD: ${yb_value_crvusd:,.2f}")
    print(f"  in {sym}: +{yb_value_btc:.8f}")
    print()
    print(f"Total integrated PnL:     "
          f"{pnl_lt_btc + pnl_gauge_btc + yb_value_btc:+.8f} {sym}  "
          f"(LT + Gauge + YB rewards)")


if __name__ == "__main__":
    main()
