"""Fetch the components of the pyUSD/crvUSD LP APR over time, via Multicall3.

The pool (0x625E…6200) pays LPs three ways; we record the raw inputs per block
and derive APRs in plot_pool_apr.py:

  1. Trading-fee APR — from pool.get_virtual_price() growth (1bp fee, but on a
     high-volume stable pool this is small-but-real ~0.5%+). Clean, like pps:
     derived later from a trailing virtual_price window.
  2. CRV gauge APR (aggregate/average, boost-agnostic):
        CRV_rate × gauge_relative_weight × CRV_price × yr / gauge_TVL
     CRV_rate = gauge.inflation_rate() (global CRV/s); relative weight from the
     GaugeController; CRV_price from Chainlink CRV/USD.
  3. YB incentive APR (the dominant term, aggregate/average):
        YB_reward_rate × YB_price × yr / gauge_TVL
     YB_reward_rate from gauge.reward_data(YB).rate (zeroed past period_finish);
     YB_price from the YB/crvUSD twocrypto pool price_oracle() (YB in crvUSD≈USD).

gauge_TVL = gauge.totalSupply() (staked LP) × virtual_price. The flow signal for
the response analysis is pool.totalSupply() (LP shares, minted/burned only on
add/remove liquidity).

Reward/price calls use allowFailure=True so blocks before the gauge / YB pool
existed simply leave those components blank (no rewards then anyway).

Output (xz-compressed CSV, default pool_apr.csv.xz), one row per block:
    block_number, timestamp, datetime_utc, virtual_price, lp_supply,
    gauge_staked, crv_rate, crv_rel_weight, crv_price,
    yb_rate, yb_period_finish, yb_price

Usage
-----
    uv run python fetch_pool_apr.py
    uv run python fetch_pool_apr.py --start 2025-10-01 --points 1500
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
import boa  # noqa: F401
from boa.rpc import EthereumRPC
from dotenv import load_dotenv
from eth_utils import keccak
from tqdm import tqdm

HERE = Path(__file__).resolve().parent

MC3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
POOL = "0x625E92624Bc2D88619ACCc1788365A69767f6200"
GAUGE = "0xf69Fb60B79E463384b40dbFDFB633AB5a863C9A2"
GAUGE_CONTROLLER = "0x2F50D538606Fa9EDD2B11E2446BEb18C9D5846bB"
YB = "0x01791F726B4103694969820be083196cC7c045fF"
YB_POOL = "0xec977F46467a3021785Cff88894886E617abd65b"  # YB/crvUSD twocrypto (price_oracle = YB in crvUSD)
CRV_USD_FEED = "0xCd627aA160A6fA45Eb793D19Ef54f5062F20f33f"  # Chainlink CRV/USD (8 dp)
DEFAULT_OUT = HERE / "pool_apr.csv.xz"

WAD = 10 ** 18
BATCH = 100
WORKERS = 32

# gauge.reward_data(token) -> (token, distributor, period_finish, rate, last_update, integral)
REWARD_DATA_TYPES = ["address", "address", "uint256", "uint256", "uint256", "uint256"]
# chainlink latestRoundData -> (roundId, answer, startedAt, updatedAt, answeredInRound)
FEED_TYPES = ["uint80", "int256", "uint256", "uint256", "uint80"]


def selector(sig: str) -> bytes:
    return bytes.fromhex(keccak(text=sig).hex()[:8])


def aggregate3_calldata() -> str:
    calls = [
        (POOL, False, selector("get_virtual_price()")),
        (POOL, False, selector("totalSupply()")),
        (GAUGE, True, selector("totalSupply()")),
        (GAUGE, True, selector("inflation_rate()")),
        (GAUGE_CONTROLLER, True,
         selector("gauge_relative_weight(address)") + eth_abi.encode(["address"], [GAUGE])),
        (GAUGE, True,
         selector("reward_data(address)") + eth_abi.encode(["address"], [YB])),
        (CRV_USD_FEED, True, selector("latestRoundData()")),
        (YB_POOL, True, selector("price_oracle()")),
        (MC3, False, selector("getCurrentBlockTimestamp()")),
    ]
    payload = eth_abi.encode(["(address,bool,bytes)[]"], [calls])
    return "0x" + (selector("aggregate3((address,bool,bytes)[])") + payload).hex()


def _uint(b: bytes) -> int:
    return int.from_bytes(b, "big") if b else 0


def decode_aggregate3(result_hex: str):
    items = eth_abi.decode(["(bool,bytes)[]"], bytes.fromhex(result_hex[2:]))[0]

    def ok(i):
        return items[i][0] and items[i][1]

    vprice = _uint(items[0][1])
    lp_supply = _uint(items[1][1])
    gauge_staked = _uint(items[2][1])
    crv_rate = _uint(items[3][1])
    rel_weight = _uint(items[4][1])
    if ok(5):
        rd = eth_abi.decode(REWARD_DATA_TYPES, items[5][1])
        yb_period, yb_rate = rd[2], rd[3]
    else:
        yb_period, yb_rate = 0, 0
    crv_price = eth_abi.decode(FEED_TYPES, items[6][1])[1] if ok(6) else 0
    yb_price = _uint(items[7][1])
    ts = _uint(items[8][1])
    return (vprice, lp_supply, gauge_staked, crv_rate, rel_weight,
            yb_rate, yb_period, crv_price, yb_price, ts)


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
    ap.add_argument("--start", default="2025-10-01")
    ap.add_argument("--end", default=None, help="default chain head")
    ap.add_argument("--points", type=int, default=1000)
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    url = os.environ.get("NETWORK")
    if not url:
        raise SystemExit("NETWORK not set (see .env / .env.example)")
    rpc = EthereumRPC(url)

    latest = int(rpc.fetch("eth_blockNumber", []), 16)
    start_block = block_for_timestamp(rpc, parse_date(args.start), 1, latest)
    end_block = (block_for_timestamp(rpc, parse_date(args.end), start_block, latest)
                 if args.end else latest)

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
        payloads = [("eth_call", [{"to": MC3, "data": data}, hex(b)]) for b in blk_list]
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
        w.writerow(["block_number", "timestamp", "datetime_utc", "virtual_price",
                    "lp_supply", "gauge_staked", "crv_rate", "crv_rel_weight",
                    "crv_price", "yb_rate", "yb_period_finish", "yb_price"])
        pbar = tqdm(total=len(blocks), unit="blk", unit_scale=True,
                    desc="pool apr", dynamic_ncols=True)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for rows in ex.map(fetch_chunk, chunks):
                for (b, vprice, lp, gst, crate, rw, ybrate, ybper, cpx, ybpx,
                     ts) in rows:
                    w.writerow([
                        b, ts, dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(),
                        vprice / WAD, lp / WAD, gst / WAD, crate / WAD, rw / WAD,
                        cpx / 1e8, ybrate / WAD, ybper, ybpx / WAD])
                pbar.update(len(rows))
                if rows:
                    pbar.set_postfix(block=rows[-1][0])
        pbar.close()

    print(f"\nwrote {len(blocks):,} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
