"""Download price_oracle() and price_scale() of a Curve twocrypto pool at every
block of its existence, with the block timestamp, via Multicall3.

Target pool (default): 0x83f24023d15d835a213df24fd309c47dAb5BEb32
  a crvUSD/cbBTC twocrypto-ng pool. price_oracle()/price_scale() are cbBTC priced
  in crvUSD (~BTC/USD), 1e18 fixed point. The matching external feed for the
  companion file is Chainlink BTC/USD (see the printed command at the end).

How it reads "in the same call together with time"
---------------------------------------------------
Each sampled block issues ONE eth_call to Multicall3.aggregate3([...]) bundling:
    pool.price_oracle(), pool.price_scale(), MC3.getCurrentBlockTimestamp()
so the three values come back atomically for that block. Many blocks are then
batched into a single JSON-RPC request via boa's EthereumRPC.fetch_multi, so the
~1.78M-block sweep is a few thousand round-trips, not millions.

The pool's lifespan is auto-detected: the inception block is found by binary
search for the first block where the pool address has code; the end is the
chain head (override with --start-block/--end-block). --stride subsamples.

Output (CSV, default pool_oracle_scale.csv), one row per sampled block:
    block_number, timestamp, datetime_utc, price_oracle, price_scale

Usage
-----
    uv run python fetch_pool_oracle.py
    uv run python fetch_pool_oracle.py --stride 10 --out pool_sparse.csv
    uv run python fetch_pool_oracle.py --start-block 23433451 --end-block 23500000
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
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

POOL = "0x83f24023d15d835a213df24fd309c47dAb5BEb32"
MC3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
DEFAULT_OUT = HERE / "pool_oracle_scale.csv"

# Chainlink BTC/USD proxy — the feed to fetch for the companion file.
CHAINLINK_BTCUSD = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"

WAD = 10 ** 18
# This node caps JSON-RPC batches at 100 (server --rpc.batch.limit), so 100 is
# the max blocks per fetch_multi request. Speed comes from running many such
# batches concurrently; throughput plateaus around 32 workers (~3.3k blk/s).
BATCH = 100
WORKERS = 32


def selector(sig: str) -> bytes:
    return bytes.fromhex(keccak(text=sig).hex()[:8])


def aggregate3_calldata() -> str:
    """Constant calldata: aggregate3([price_oracle, price_scale, timestamp])."""
    calls = [
        (POOL, False, selector("price_oracle()")),
        (POOL, False, selector("price_scale()")),
        (MC3, False, selector("getCurrentBlockTimestamp()")),
    ]
    payload = eth_abi.encode(["(address,bool,bytes)[]"], [calls])
    return "0x" + (selector("aggregate3((address,bool,bytes)[])") + payload).hex()


def decode_aggregate3(result_hex: str) -> tuple[int, int, int]:
    """-> (price_oracle_raw, price_scale_raw, timestamp)."""
    items = eth_abi.decode(["(bool,bytes)[]"], bytes.fromhex(result_hex[2:]))[0]
    po = int.from_bytes(items[0][1], "big")
    ps = int.from_bytes(items[1][1], "big")
    ts = int.from_bytes(items[2][1], "big")
    return po, ps, ts


def has_code(rpc: EthereumRPC, addr: str, block: int) -> bool:
    return rpc.fetch("eth_getCode", [addr, hex(block)]) not in ("0x", "0x0", "")


def find_inception(rpc: EthereumRPC, addr: str, latest: int) -> int:
    """First block where `addr` has code (binary search)."""
    if not has_code(rpc, addr, latest):
        raise RuntimeError(f"{addr} has no code at head block {latest}")
    lo, hi = 1, latest
    with tqdm(total=hi - lo, desc="find inception", unit="blk",
              unit_scale=True, leave=False, dynamic_ncols=True) as pbar:
        while lo < hi:
            mid = (lo + hi) // 2
            if has_code(rpc, addr, mid):
                hi = mid
            else:
                lo = mid + 1
            pbar.n = (hi - lo)
            pbar.set_postfix_str(f"~{lo}")
            pbar.refresh()
    return lo


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", default=POOL, help="twocrypto pool address")
    ap.add_argument("--start-block", type=int, default=None,
                    help="default: auto-detected pool inception block")
    ap.add_argument("--end-block", type=int, default=None,
                    help="default: current chain head")
    ap.add_argument("--stride", type=int, default=1,
                    help="sample every Nth block (default 1 = every block)")
    ap.add_argument("--batch", type=int, default=BATCH,
                    help=f"blocks per JSON-RPC batch, node max 100 (default {BATCH})")
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help=f"concurrent batch requests (default {WORKERS})")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    url = os.environ.get("ETH_RPC_URL")
    if not url:
        raise SystemExit("ETH_RPC_URL not set (see .env / .env.example)")
    rpc = EthereumRPC(url)

    latest = int(rpc.fetch("eth_blockNumber", []), 16)
    end_block = args.end_block if args.end_block is not None else latest
    if args.start_block is not None:
        start_block = args.start_block
    else:
        print("detecting pool inception block …", flush=True)
        start_block = find_inception(rpc, args.pool, latest)

    def blk_ts(b):
        return int(rpc.fetch("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16)
    t0, t1 = blk_ts(start_block), blk_ts(end_block)
    blocks = list(range(start_block, end_block + 1, args.stride))
    print(f"pool:   {args.pool}")
    print(f"blocks: {start_block} .. {end_block}  stride={args.stride}  "
          f"-> {len(blocks):,} samples")
    print(f"window: {dt.datetime.fromtimestamp(t0, dt.UTC)} .. "
          f"{dt.datetime.fromtimestamp(t1, dt.UTC)}  (unix {t0}..{t1})")

    data = aggregate3_calldata()
    batch = min(args.batch, 100)  # node hard limit
    chunks = [blocks[i:i + batch] for i in range(0, len(blocks), batch)]

    # One RPC connection per worker thread (the client isn't shared-safe).
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

    print(f"fetching with {args.workers} workers x batch {batch} …", flush=True)
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["block_number", "timestamp", "datetime_utc",
                    "price_oracle", "price_scale"])
        pbar = tqdm(total=len(blocks), unit="blk", unit_scale=True,
                    desc="price_oracle/scale", dynamic_ncols=True)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            # ex.map preserves input order -> rows stay sorted by block.
            for rows in ex.map(fetch_chunk, chunks):
                for b, po, ps, ts in rows:
                    w.writerow([
                        b, ts,
                        dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(),
                        po / WAD, ps / WAD,
                    ])
                pbar.update(len(rows))
                if rows:
                    pbar.set_postfix(block=rows[-1][0])
        pbar.close()

    print(f"\nwrote {len(blocks):,} rows -> {args.out}")
    print("\nCompanion Chainlink BTC/USD over the SAME period — run:")
    print(f"  uv run python fetch_chainlink.py --start {t0} --end {t1} "
          f"--out chainlink_pool_window.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
