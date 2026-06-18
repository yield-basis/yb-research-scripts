"""Fetch typical market lending/borrow rates over time, via Multicall3.

Three rate sources, sampled at N time-spaced blocks over a date range:

  1. LlamaLend WBTC mint-market borrow rate
       AMM.rate()  (0xE0438Eb3703bF871E31Ce639bd351109c88666ea)
     Returns the per-second rate at which debt grows, 1e18 fixed point. This is
     the realized borrow rate (the monetary policy 0x07491D… writes it into the
     AMM; Controller is 0x4e595413…). APR = rate * SECONDS_PER_YEAR.

  2. scrvUSD savings rate
       scrvUSD.convertToAssets(1e18)  (0x0655977FEb2f289A4aB78af67BAB0d17aAb84367)
     scrvUSD is an ERC4626 vault; convertToAssets(1e18) is its price-per-share
     (crvUSD per share, 1e18 fp). There is no instantaneous on-chain "rate", so
     the APR is derived here from the growth of price-per-share between samples:
         apr = (pps[i]/pps[i-1] - 1) / dt_seconds * SECONDS_PER_YEAR
     (the vault unlocks profit linearly between harvests, so this local slope is
     a clean realized rate away from harvest steps).

  3. Aave v3 USDC supply rate
       PoolDataProvider.getReserveData(USDC)[5] = currentLiquidityRate
       PoolDataProvider 0x7B4EB56E7CD4b454BA8ff71E4518426369a138a3
       USDC 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
     Aave expresses the liquidity (supply) rate in ray (1e27) as an annual APR
     already, so supply APR = liquidityRate / 1e27.

Each sampled block issues ONE eth_call to Multicall3.aggregate3([...]) bundling
the three reads plus MC3.getCurrentBlockTimestamp(), so all come back atomically.
Blocks are batched 100/request via boa's EthereumRPC.fetch_multi, many batches
concurrently.

Output (xz-compressed CSV, default market_rates.csv.xz), one row per block:
    block_number, timestamp, datetime_utc,
    llama_rate_per_sec, llama_apr,
    scrvusd_pps, scrvusd_apr,
    aave_usdc_liquidity_rate_ray, aave_usdc_apr

Usage
-----
    uv run python fetch_market_rates.py
    uv run python fetch_market_rates.py --points 2000 --start 2025-10-01 --end 2026-06-17
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
LLAMA_AMM = "0xE0438Eb3703bF871E31Ce639bd351109c88666ea"
SCRVUSD = "0x0655977FEb2f289A4aB78af67BAB0d17aAb84367"
AAVE_DP = "0x7B4EB56E7CD4b454BA8ff71E4518426369a138a3"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
DEFAULT_OUT = HERE / "market_rates.csv.xz"

WAD = 10 ** 18
RAY = 10 ** 27
SECONDS_PER_YEAR = 365 * 86400

# Aave v3 PoolDataProvider.getReserveData return layout (currentLiquidityRate = [5]).
AAVE_RESERVE_TYPES = (
    ["uint256"] * 11 + ["uint40"]
)

BATCH = 100
WORKERS = 32


def selector(sig: str) -> bytes:
    return bytes.fromhex(keccak(text=sig).hex()[:8])


def aggregate3_calldata() -> str:
    """aggregate3([llama.rate(), scrvUSD.convertToAssets(1e18),
    aaveDP.getReserveData(USDC), MC3.timestamp])."""
    calls = [
        (LLAMA_AMM, False, selector("rate()")),
        (SCRVUSD, False,
         selector("convertToAssets(uint256)") + eth_abi.encode(["uint256"], [WAD])),
        (AAVE_DP, False,
         selector("getReserveData(address)") + eth_abi.encode(["address"], [USDC])),
        (MC3, False, selector("getCurrentBlockTimestamp()")),
    ]
    payload = eth_abi.encode(["(address,bool,bytes)[]"], [calls])
    return "0x" + (selector("aggregate3((address,bool,bytes)[])") + payload).hex()


def decode_aggregate3(result_hex: str) -> tuple[int, int, int, int]:
    """-> (llama_rate_raw, scrvusd_pps_raw, aave_liquidity_rate_ray, timestamp)."""
    items = eth_abi.decode(["(bool,bytes)[]"], bytes.fromhex(result_hex[2:]))[0]
    llama = int.from_bytes(items[0][1], "big")
    pps = int.from_bytes(items[1][1], "big")
    aave = eth_abi.decode(AAVE_RESERVE_TYPES, items[2][1])[5]
    ts = int.from_bytes(items[3][1], "big")
    return llama, pps, aave, ts


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
    ap.add_argument("--start", default="2025-10-01", help="start date (UTC, ISO)")
    ap.add_argument("--end", default="2026-06-17", help="end date (UTC, ISO)")
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
    t_start, t_end = parse_date(args.start), parse_date(args.end)

    print("locating start/end blocks by timestamp …", flush=True)
    start_block = block_for_timestamp(rpc, t_start, 1, latest)
    end_block = block_for_timestamp(rpc, t_end, start_block, latest)

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
    # Collect first (need previous pps to derive scrvUSD APR), then write.
    rows = []
    pbar = tqdm(total=len(blocks), unit="blk", unit_scale=True,
                desc="market rates", dynamic_ncols=True)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for chunk_rows in ex.map(fetch_chunk, chunks):
            rows.extend(chunk_rows)
            pbar.update(len(chunk_rows))
            if chunk_rows:
                pbar.set_postfix(block=chunk_rows[-1][0])
    pbar.close()

    with lzma.open(args.out, "wt", newline="", preset=6) as fh:
        w = csv.writer(fh)
        w.writerow(["block_number", "timestamp", "datetime_utc",
                    "llama_rate_per_sec", "llama_apr",
                    "scrvusd_pps", "scrvusd_apr",
                    "aave_usdc_liquidity_rate_ray", "aave_usdc_apr"])
        prev_pps = prev_ts = None
        for b, llama, pps, aave, ts in rows:
            llama_apr = llama / WAD * SECONDS_PER_YEAR
            aave_apr = aave / RAY
            if prev_pps is not None and prev_ts is not None and ts > prev_ts:
                scrv_apr = (pps / prev_pps - 1.0) / (ts - prev_ts) * SECONDS_PER_YEAR
            else:
                scrv_apr = ""  # first sample: no previous point to diff against
            prev_pps, prev_ts = pps, ts
            w.writerow([
                b, ts, dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(),
                llama, llama_apr,
                pps, scrv_apr,
                aave, aave_apr,
            ])

    print(f"\nwrote {len(rows):,} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
