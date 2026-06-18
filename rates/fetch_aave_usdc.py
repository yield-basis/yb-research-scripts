"""Fetch Aave v3 USDC supply rate (APR) over a long window, via Multicall3.

Aave v3 on Ethereum launched Jan 2023 with USDC as a reserve, so the v3
PoolDataProvider covers 2024-01-01 .. now with no need to merge in Aave v2.

Read per sampled block:
    Pool.getReserveData(USDC).currentLiquidityRate (ray, 1e27) = struct field [2]
        Pool 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2
        USDC 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
Aave expresses the liquidity (supply) rate in ray as an annual APR already, so
    supply APR = currentLiquidityRate / 1e27.

The canonical Pool is read directly rather than the AaveProtocolDataProvider:
the data provider reverts for ~6 days during Aave's Oct-2024 upgrade window,
while the Pool returns valid data throughout.

Each sampled block issues ONE eth_call to Multicall3.aggregate3([getReserveData,
getCurrentBlockTimestamp]); blocks are batched 100/request via boa's
EthereumRPC.fetch_multi, many batches concurrently.

Output (xz-compressed CSV, default aave_usdc_rates.csv.xz), one row per block:
    block_number, timestamp, datetime_utc, liquidity_rate_ray, aave_usdc_apr

Usage
-----
    uv run python fetch_aave_usdc.py
    uv run python fetch_aave_usdc.py --start 2024-01-01 --end 2026-06-18 --points 1500
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

MC3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
AAVE_POOL = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
DEFAULT_OUT = HERE / "aave_usdc_rates.csv.xz"

RAY = 10 ** 27
# Aave v3 Pool.getReserveData(asset) struct layout. currentLiquidityRate = [2].
AAVE_RESERVE_TYPES = [
    "uint256",  # configuration
    "uint128",  # liquidityIndex
    "uint128",  # currentLiquidityRate   <- [2]
    "uint128",  # variableBorrowIndex
    "uint128",  # currentVariableBorrowRate
    "uint128",  # currentStableBorrowRate
    "uint40",   # lastUpdateTimestamp
    "uint16",   # id
    "address",  # aTokenAddress
    "address",  # stableDebtTokenAddress
    "address",  # variableDebtTokenAddress
    "address",  # interestRateStrategyAddress
    "uint128",  # accruedToTreasury
    "uint128",  # unbacked
    "uint128",  # isolationModeTotalDebt
]
AAVE_RATE_IDX = 2

BATCH = 100
WORKERS = 32


def selector(sig: str) -> bytes:
    return bytes.fromhex(keccak(text=sig).hex()[:8])


def aggregate3_calldata() -> str:
    # allowFailure=True on the Aave call so an isolated reverting block (rare over
    # a multi-year sweep) becomes a gap instead of aborting the whole batch.
    calls = [
        (AAVE_POOL, True,
         selector("getReserveData(address)") + eth_abi.encode(["address"], [USDC])),
        (MC3, False, selector("getCurrentBlockTimestamp()")),
    ]
    payload = eth_abi.encode(["(address,bool,bytes)[]"], [calls])
    return "0x" + (selector("aggregate3((address,bool,bytes)[])") + payload).hex()


def decode_aggregate3(result_hex: str) -> tuple[int | None, int]:
    """-> (liquidity_rate_ray or None on failure, timestamp)."""
    items = eth_abi.decode(["(bool,bytes)[]"], bytes.fromhex(result_hex[2:]))[0]
    ok, ret = items[0]
    rate = eth_abi.decode(AAVE_RESERVE_TYPES, ret)[AAVE_RATE_IDX] if ok and ret else None
    ts = int.from_bytes(items[1][1], "big")
    return rate, ts


def blk_ts(rpc: EthereumRPC, b: int) -> int:
    return int(rpc.fetch("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16)


def block_for_timestamp(rpc: EthereumRPC, target: int, lo: int, hi: int) -> int:
    while lo < hi:
        mid = (lo + hi) // 2
        if blk_ts(rpc, mid) < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def parse_date(s: str) -> int:
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return int(d.timestamp())


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2024-01-01", help="start date (UTC, ISO)")
    ap.add_argument("--end", default=None, help="end date (UTC, ISO); default chain head")
    ap.add_argument("--points", type=int, default=1000,
                    help="number of time-spaced samples (default 1000)")
    ap.add_argument("--batch", type=int, default=BATCH,
                    help=f"blocks per JSON-RPC batch, node max 100 (default {BATCH})")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    url = os.environ.get("NETWORK")
    if not url:
        raise SystemExit("NETWORK not set (see .env / .env.example)")
    rpc = EthereumRPC(url)

    latest = int(rpc.fetch("eth_blockNumber", []), 16)
    t_start = parse_date(args.start)

    print("locating start/end blocks by timestamp …", flush=True)
    start_block = block_for_timestamp(rpc, t_start, 1, latest)
    if args.end is not None:
        end_block = block_for_timestamp(rpc, parse_date(args.end), start_block, latest)
    else:
        end_block = latest

    n = args.points
    if n < 2:
        raise SystemExit("--points must be >= 2")
    span = end_block - start_block
    blocks = sorted({start_block + (span * k) // (n - 1) for k in range(n)})

    t0, t1 = blk_ts(rpc, start_block), blk_ts(rpc, end_block)
    print(f"blocks: {start_block} .. {end_block}  -> {len(blocks):,} samples")
    print(f"window: {dt.datetime.fromtimestamp(t0, dt.UTC)} .. "
          f"{dt.datetime.fromtimestamp(t1, dt.UTC)}")

    data = aggregate3_calldata()
    batch = min(args.batch, 100)
    chunks = [blocks[i:i + batch] for i in range(0, len(blocks), batch)]
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
            except Exception as e:
                last = e
                time.sleep(0.25 * (attempt + 1))
        raise RuntimeError(f"batch at block {blk_list[0]} failed: {last}")

    print(f"fetching with {args.workers} workers x batch {batch} …", flush=True)
    with lzma.open(args.out, "wt", newline="", preset=6) as fh:
        w = csv.writer(fh)
        w.writerow(["block_number", "timestamp", "datetime_utc",
                    "liquidity_rate_ray", "aave_usdc_apr"])
        pbar = tqdm(total=len(blocks), unit="blk", unit_scale=True,
                    desc="aave usdc", dynamic_ncols=True)
        failed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for rows in ex.map(fetch_chunk, chunks):
                for b, rate, ts in rows:
                    if rate is None:
                        failed += 1
                        w.writerow([
                            b, ts,
                            dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(), "", ""])
                    else:
                        w.writerow([
                            b, ts,
                            dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(),
                            rate, rate / RAY])
                pbar.update(len(rows))
                if rows:
                    pbar.set_postfix(block=rows[-1][0])
        pbar.close()

    print(f"\nwrote {len(blocks):,} rows -> {args.out}  ({failed} blocks reverted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
