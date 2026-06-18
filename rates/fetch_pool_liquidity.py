"""Fetch total liquidity (balances(0)+balances(1)) of a Curve stableswap pool
over time, sampled at N points evenly spaced across a date range, via Multicall3.

Target pool (default): 0x625E92624Bc2D88619ACCc1788365A69767f6200
  a PYUSD/crvUSD stableswap pool. balances(0) is PYUSD (6 decimals), balances(1)
  is crvUSD (18 decimals); both ~$1, so total USD liquidity is
      balances(0)/1e6 + balances(1)/1e18.

How time is read together with the balances
-------------------------------------------
Each sampled block issues ONE eth_call to Multicall3.aggregate3([...]) bundling:
    pool.balances(0), pool.balances(1), MC3.getCurrentBlockTimestamp()
so the three values come back atomically for that block. Many blocks are batched
into a single JSON-RPC request via boa's EthereumRPC.fetch_multi.

Sampling is in *time*, not block number: the blocks for the start/end dates are
found by binary search on block timestamp, then N block numbers are linearly
spaced between them (block time is ~constant, so this is near-uniform in time;
the exact timestamp of each sample is recorded regardless).

Output (xz-compressed CSV, default pool_liquidity.csv.xz), one row per block:
    block_number, timestamp, datetime_utc, balance0_raw, balance1_raw, liquidity

Usage
-----
    uv run python fetch_pool_liquidity.py
    uv run python fetch_pool_liquidity.py --points 2000 --start 2025-10-01 --end 2026-06-17
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import lzma
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import eth_abi
import boa  # noqa: F401  (kept so the env/.env story matches the other scripts)
from boa.rpc import EthereumRPC
from dotenv import load_dotenv
from eth_utils import keccak
from tqdm import tqdm

HERE = Path(__file__).resolve().parent

POOL = "0x625E92624Bc2D88619ACCc1788365A69767f6200"
MC3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
DEFAULT_OUT = HERE / "pool_liquidity.csv.xz"

# Token decimals for balances(0), balances(1): PYUSD=6, crvUSD=18.
DECIMALS = (6, 18)

# Node caps JSON-RPC batches at 100; speed comes from many concurrent batches.
BATCH = 100
WORKERS = 32


def selector(sig: str) -> bytes:
    return bytes.fromhex(keccak(text=sig).hex()[:8])


def balances_calldata(i: int) -> bytes:
    return selector("balances(uint256)") + eth_abi.encode(["uint256"], [i])


def aggregate3_calldata() -> str:
    """Constant calldata: aggregate3([balances(0), balances(1), timestamp])."""
    calls = [
        (POOL, False, balances_calldata(0)),
        (POOL, False, balances_calldata(1)),
        (MC3, False, selector("getCurrentBlockTimestamp()")),
    ]
    payload = eth_abi.encode(["(address,bool,bytes)[]"], [calls])
    return "0x" + (selector("aggregate3((address,bool,bytes)[])") + payload).hex()


def decode_aggregate3(result_hex: str) -> tuple[int, int, int]:
    """-> (balance0_raw, balance1_raw, timestamp)."""
    items = eth_abi.decode(["(bool,bytes)[]"], bytes.fromhex(result_hex[2:]))[0]
    b0 = int.from_bytes(items[0][1], "big")
    b1 = int.from_bytes(items[1][1], "big")
    ts = int.from_bytes(items[2][1], "big")
    return b0, b1, ts


def blk_ts(rpc: EthereumRPC, b: int) -> int:
    return int(rpc.fetch("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16)


def block_for_timestamp(rpc: EthereumRPC, target: int, lo: int, hi: int) -> int:
    """First block whose timestamp >= target (binary search in [lo, hi])."""
    while lo < hi:
        mid = (lo + hi) // 2
        if blk_ts(rpc, mid) < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def parse_date(s: str) -> int:
    """ISO date/datetime -> unix timestamp (UTC)."""
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return int(d.timestamp())


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", default=POOL, help="stableswap pool address")
    ap.add_argument("--start", default="2025-10-01", help="start date (UTC, ISO)")
    ap.add_argument("--end", default="2026-06-17", help="end date (UTC, ISO)")
    ap.add_argument("--points", type=int, default=1000,
                    help="number of time-spaced samples (default 1000)")
    ap.add_argument("--batch", type=int, default=BATCH,
                    help=f"blocks per JSON-RPC batch, node max 100 (default {BATCH})")
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help=f"concurrent batch requests (default {WORKERS})")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    url = os.environ.get("NETWORK")
    if not url:
        raise SystemExit("NETWORK not set (see .env / .env.example)")
    rpc = EthereumRPC(url)

    latest = int(rpc.fetch("eth_blockNumber", []), 16)
    t_start, t_end = parse_date(args.start), parse_date(args.end)

    print("locating start/end blocks by timestamp …", flush=True)
    start_block = block_for_timestamp(rpc, t_start, 1, latest)
    end_block = block_for_timestamp(rpc, t_end, start_block, latest)

    # N block numbers linearly spaced in [start_block, end_block].
    n = args.points
    if n < 2:
        raise SystemExit("--points must be >= 2")
    span = end_block - start_block
    blocks = sorted({start_block + (span * k) // (n - 1) for k in range(n)})

    t0, t1 = blk_ts(rpc, start_block), blk_ts(rpc, end_block)
    print(f"pool:   {args.pool}")
    print(f"blocks: {start_block} .. {end_block}  -> {len(blocks):,} samples")
    print(f"window: {dt.datetime.fromtimestamp(t0, dt.UTC)} .. "
          f"{dt.datetime.fromtimestamp(t1, dt.UTC)}")

    data = aggregate3_calldata()
    batch = min(args.batch, 100)  # node hard limit
    chunks = [blocks[i:i + batch] for i in range(0, len(blocks), batch)]

    # One RPC connection per worker thread (client isn't shared-safe).
    tl = threading.local()

    def fetch_chunk(blk_list):
        rpc_t = getattr(tl, "rpc", None)
        if rpc_t is None:
            rpc_t = tl.rpc = EthereumRPC(url)
        payloads = [("eth_call", [{"to": MC3, "data": data}, hex(b)])
                    for b in blk_list]
        last = None
        for attempt in range(4):
            try:
                results = rpc_t.fetch_multi(payloads)
                return [(b, *decode_aggregate3(r)) for b, r in zip(blk_list, results)]
            except Exception as e:  # transient; back off and retry
                last = e
                time.sleep(0.25 * (attempt + 1))
        raise RuntimeError(f"batch at block {blk_list[0]} failed: {last}")

    s0, s1 = 10 ** DECIMALS[0], 10 ** DECIMALS[1]
    print(f"fetching with {args.workers} workers x batch {batch} …", flush=True)
    with lzma.open(args.out, "wt", newline="", preset=6) as fh:
        w = csv.writer(fh)
        w.writerow(["block_number", "timestamp", "datetime_utc",
                    "balance0_raw", "balance1_raw", "liquidity"])
        pbar = tqdm(total=len(blocks), unit="blk", unit_scale=True,
                    desc="liquidity", dynamic_ncols=True)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            # ex.map preserves input order -> rows stay sorted by block.
            for rows in ex.map(fetch_chunk, chunks):
                for b, b0, b1, ts in rows:
                    liq = b0 / s0 + b1 / s1
                    w.writerow([
                        b, ts,
                        dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(),
                        b0, b1, liq,
                    ])
                pbar.update(len(rows))
                if rows:
                    pbar.set_postfix(block=rows[-1][0])
        pbar.close()

    print(f"\nwrote {len(blocks):,} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
