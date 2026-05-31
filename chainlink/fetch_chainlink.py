"""Fetch the Chainlink BTC/USD on-chain feed over the candle time range.

What "every block" means here
-----------------------------
The Chainlink aggregator only writes a new answer on an `AnswerUpdated` event
(deviation threshold or heartbeat). Between two updates `latestRoundData()`
returns the *same* value at every block. So the full per-block series is exactly
the step function defined by the round updates: forward-fill the last update's
answer across every block until the next one. We therefore fetch every
`AnswerUpdated` round in the window (a few thousand rows) instead of issuing one
eth_call per block (~5.4M). The CSV is the compact change-point series; to get a
value "at block B" or "at candle minute T", as-of join (forward fill) on
`block_number` / `updated_at`.

Feed
----
Chainlink BTC/USD proxy on Ethereum mainnet:
    0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c   (8 decimals)
The proxy delegates to a phase aggregator; the actual `AnswerUpdated` logs are
emitted by the underlying aggregator(s). We resolve every phase aggregator that
was active in the window via `phaseId()` / `phaseAggregators(uint16)` and scan
each, so phase transitions inside the window are handled.

Time range
----------
Defaults to the exact span of the candle file (`btcusdt-2024-F2026.json.xz`):
2024-01-24 00:00:00 UTC .. 2026-02-15 23:59:00 UTC. Override with --start/--end
(unix seconds) or --candles.

Chain access
------------
Uses titanoboa: `boa.fork` + an ABI contract for typed metadata reads, and the
underlying `EthereumRPC` for raw eth_getLogs / eth_getBlockByNumber / batched
eth_call plumbing. RPC URL from `ETH_RPC_URL` in .env.

Usage
-----
    uv run python fetch_chainlink.py
    uv run python fetch_chainlink.py --out chainlink_btcusd_rounds.csv
    uv run python fetch_chainlink.py --start 1706054400 --end 1771199940

Output: chainlink_btcusd_rounds.csv with one row per AnswerUpdated round:
    phase_id, agg_round_id, block_number, updated_at, datetime_utc,
    answer_raw, price
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import lzma
import json
import os
import sys
from pathlib import Path

import boa
from boa.rpc import EthereumRPC
from dotenv import load_dotenv
from eth_utils import keccak
from tqdm import tqdm

HERE = Path(__file__).resolve().parent

# Chainlink BTC/USD proxy (Ethereum mainnet).
PROXY = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"

# AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt)
ANSWER_UPDATED_TOPIC = "0x" + keccak(
    text="AnswerUpdated(int256,uint256,uint256)"
).hex()

# Minimal proxy ABI for the metadata reads we need.
PROXY_ABI = json.dumps([
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "description", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "phaseId", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint16"}]},
    {"name": "phaseAggregators", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "phaseId", "type": "uint16"}],
     "outputs": [{"type": "address"}]},
    {"name": "aggregator", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address"}]},
])

DEFAULT_CANDLES = HERE / "btcusdt-2024-F2026.json.xz"
DEFAULT_OUT = HERE / "chainlink_btcusd_rounds.csv"

# eth_getLogs chunk; a local node handles large ranges, but we stay polite and
# back off on provider errors. Smaller chunks => smoother progress bar.
LOG_CHUNK = 50_000


def hex_(n: int) -> str:
    return hex(n)


def to_int256(topic: str) -> int:
    """Decode a 32-byte hex topic as a signed int256."""
    v = int(topic, 16)
    if v >= 1 << 255:
        v -= 1 << 256
    return v


def candle_time_range(path: Path) -> tuple[int, int]:
    """First and last candle open-times (unix seconds) from the .xz file."""
    with lzma.open(path) as fh:
        data = json.load(fh)
    return data[0][0] // 1000, data[-1][0] // 1000


def block_timestamp(rpc: EthereumRPC, block: int) -> int:
    blk = rpc.fetch("eth_getBlockByNumber", [hex_(block), False])
    return int(blk["timestamp"], 16)


def find_block_at_or_after(rpc: EthereumRPC, ts: int, lo: int, hi: int) -> int:
    """Smallest block number whose timestamp >= ts (binary search)."""
    span = hi - lo + 1
    with tqdm(total=span, desc=f"locate block @ {ts}", unit="blk",
              unit_scale=True, leave=False, dynamic_ncols=True) as pbar:
        while lo < hi:
            mid = (lo + hi) // 2
            if block_timestamp(rpc, mid) < ts:
                lo = mid + 1
            else:
                hi = mid
            pbar.n = span - (hi - lo + 1)
            pbar.set_postfix_str(f"~block {lo}")
            pbar.refresh()
    return lo


def find_block_at_or_before(rpc: EthereumRPC, ts: int, lo: int, hi: int) -> int:
    """Largest block number whose timestamp <= ts (binary search)."""
    span = hi - lo + 1
    res = lo
    with tqdm(total=span, desc=f"locate block @ {ts}", unit="blk",
              unit_scale=True, leave=False, dynamic_ncols=True) as pbar:
        while lo <= hi:
            mid = (lo + hi) // 2
            if block_timestamp(rpc, mid) <= ts:
                res = mid
                lo = mid + 1
            else:
                hi = mid - 1
            pbar.n = span - (hi - lo + 1)
            pbar.set_postfix_str(f"~block {res}")
            pbar.refresh()
    return res


def call_proxy(rpc: EthereumRPC, selector_data: str, block: int | str = "latest") -> str:
    return rpc.fetch("eth_call", [{"to": PROXY, "data": selector_data}, block])


def phase_aggregators_in_window(
    proxy, rpc: EthereumRPC, start_block: int, end_block: int
) -> list[tuple[int, str]]:
    """(phase_id, aggregator_address) for every phase active in the window.

    phaseId is read at both ends of the window (it only ever increases), and we
    resolve each phaseAggregators(p) in that inclusive range.
    """
    # phaseId() selector via raw call at specific blocks (read at the bounds).
    sel = "0x" + keccak(text="phaseId()").hex()[:8]
    p_start = int(call_proxy(rpc, sel, hex_(start_block)), 16)
    p_end = int(call_proxy(rpc, sel, hex_(end_block)), 16)
    out = []
    for p in range(p_start, p_end + 1):
        agg = str(proxy.phaseAggregators(p))
        if int(agg, 16) != 0:
            out.append((p, agg))
    return out


def fetch_answer_updated(
    rpc: EthereumRPC, aggregator: str, from_block: int, to_block: int
) -> list[dict]:
    """All AnswerUpdated logs for one aggregator in [from_block, to_block]."""
    rows: list[dict] = []
    chunk = LOG_CHUNK
    b = from_block
    pbar = tqdm(total=to_block - from_block + 1, unit="blk", unit_scale=True,
                desc=f"AnswerUpdated {aggregator[:10]}…", leave=True,
                dynamic_ncols=True)
    while b <= to_block:
        hi = min(b + chunk - 1, to_block)
        params = [{
            "fromBlock": hex_(b),
            "toBlock": hex_(hi),
            "address": aggregator,
            "topics": [ANSWER_UPDATED_TOPIC],
        }]
        try:
            logs = rpc.fetch("eth_getLogs", params)
        except Exception as e:  # provider range/size limit -> shrink and retry
            if chunk > 1000:
                chunk //= 2
                pbar.set_postfix_str(f"shrank chunk→{chunk}")
                continue
            raise RuntimeError(f"eth_getLogs failed at {b}-{hi}: {e}") from e
        for lg in logs:
            rows.append({
                "block_number": int(lg["blockNumber"], 16),
                "answer_raw": to_int256(lg["topics"][1]),
                "agg_round_id": int(lg["topics"][2], 16),
                "updated_at": int(lg["data"][2:66], 16),
            })
        pbar.update(hi - b + 1)
        pbar.set_postfix(rounds=len(rows), block=hi)
        b = hi + 1
    pbar.close()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=int, default=None,
                    help="start unix seconds (default: first candle)")
    ap.add_argument("--end", type=int, default=None,
                    help="end unix seconds (default: last candle)")
    ap.add_argument("--candles", type=Path, default=DEFAULT_CANDLES,
                    help="candle .xz file to derive the default range from")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    url = os.environ.get("ETH_RPC_URL")
    if not url:
        sys.exit("ETH_RPC_URL not set (see .env / .env.example)")

    # Resolve time range.
    if args.start is not None and args.end is not None:
        start_ts, end_ts = args.start, args.end
    else:
        print(f"reading candle range from {args.candles.name} …", flush=True)
        c_start, c_end = candle_time_range(args.candles)
        start_ts = args.start if args.start is not None else c_start
        end_ts = args.end if args.end is not None else c_end

    print(f"connecting to {url} (boa fork) …", flush=True)
    rpc = EthereumRPC(url)
    boa.fork(url, block_identifier="latest")
    proxy = boa.loads_abi(PROXY_ABI).at(PROXY)

    decimals = proxy.decimals()
    scale = 10 ** decimals
    try:
        desc = proxy.description()
    except Exception:
        desc = "BTC / USD"
    latest_block = int(rpc.fetch("eth_blockNumber", []), 16)

    print(f"feed:        {desc}  proxy={PROXY}  decimals={decimals}")
    print(f"time range:  {dt.datetime.fromtimestamp(start_ts, dt.UTC)} .. "
          f"{dt.datetime.fromtimestamp(end_ts, dt.UTC)}  (unix {start_ts}..{end_ts})")
    print(f"latest block on node: {latest_block}")

    start_block = find_block_at_or_after(rpc, start_ts, 1, latest_block)
    end_block = find_block_at_or_before(rpc, end_ts, 1, latest_block)
    print(f"block range: {start_block} .. {end_block} "
          f"({end_block - start_block + 1:,} blocks)")

    aggs = phase_aggregators_in_window(proxy, rpc, start_block, end_block)
    print(f"phase aggregators in window: "
          + ", ".join(f"p{p}={a}" for p, a in aggs))

    all_rows: list[dict] = []
    for phase_id, agg in aggs:
        rows = fetch_answer_updated(rpc, agg, start_block, end_block)
        for r in rows:
            r["phase_id"] = phase_id
        all_rows.extend(rows)
        print(f"  phase {phase_id} {agg}: {len(rows):,} AnswerUpdated rounds")

    # Clamp to the time window (updatedAt is the block timestamp of the update),
    # sort chronologically, drop any dupes across phase-boundary overlaps.
    all_rows = [r for r in all_rows if start_ts <= r["updated_at"] <= end_ts]
    all_rows.sort(key=lambda r: (r["block_number"], r["agg_round_id"]))
    seen = set()
    deduped = []
    for r in all_rows:
        key = (r["phase_id"], r["agg_round_id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    all_rows = deduped

    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["phase_id", "agg_round_id", "block_number",
                    "updated_at", "datetime_utc", "answer_raw", "price"])
        for r in all_rows:
            w.writerow([
                r["phase_id"], r["agg_round_id"], r["block_number"],
                r["updated_at"],
                dt.datetime.fromtimestamp(r["updated_at"], dt.UTC).isoformat(),
                r["answer_raw"], r["answer_raw"] / scale,
            ])

    print(f"\nwrote {len(all_rows):,} rounds -> {args.out}")
    if all_rows:
        print(f"first: block {all_rows[0]['block_number']} "
              f"price {all_rows[0]['answer_raw'] / scale:,.2f}")
        print(f"last:  block {all_rows[-1]['block_number']} "
              f"price {all_rows[-1]['answer_raw'] / scale:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
