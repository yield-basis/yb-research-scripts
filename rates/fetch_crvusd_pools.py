"""Aggregate TVL + incentives across ALL Curve crvUSD/scrvUSD pools, over time.

Tests whether the pyUSD "rush" is real new liquidity or just crvUSD rotating between
pools: if individual pools rush in but the *sum* of crvUSD across all Curve pools
does not, the rush is rotation and must not be relied on for the supply sink.

Pool list: Curve getPools API (all registries), filtered to pools whose coins include
crvUSD or scrvUSD. For each block we sum, via Multicall3:

  TVL        = Σ crvUSD.balanceOf(pool) + Σ scrvUSD.balanceOf(pool)·pps   [crvUSD-equiv]
  CRV value  = CRV.rate() · Σ gauge_relative_weight(gauge) · CRV_price · yr
  YB value   = Σ gauge.reward_data(YB).rate (gated by period_finish) · YB_price · yr

(Same incentive convention as fetch_pool_apr.py: CRV emissions + the custom YB
campaign. Other per-pool extra rewards are ignored — CRV+YB dominate crvUSD pools.)

Output (xz CSV, default crvusd_pools.csv.xz), one row per block:
    block_number, timestamp, datetime_utc, crvusd_tvl, scrvusd_bal, scrvusd_pps,
    crv_rate, sum_rel_weight, crv_price, yb_sum_rate, yb_price, n_pools, n_gauges

Usage
-----
    uv run python fetch_crvusd_pools.py
    uv run python fetch_crvusd_pools.py --start 2025-10-01 --points 1500
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
import requests
from boa.rpc import EthereumRPC
from dotenv import load_dotenv
from eth_utils import keccak

HERE = Path(__file__).resolve().parent

MC3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
SCRVUSD = "0x0655977FEb2f289A4aB78af67BAB0d17aAb84367"
CRV = "0xD533a949740bb3306d119CC777fa900bA034cd52"
GAUGE_CONTROLLER = "0x2F50D538606Fa9EDD2B11E2446BEb18C9D5846bB"
CRV_USD_FEED = "0xCd627aA160A6fA45Eb793D19Ef54f5062F20f33f"  # Chainlink CRV/USD (8 dp)
YB = "0x01791F726B4103694969820be083196cC7c045fF"
YB_POOL = "0xec977F46467a3021785Cff88894886E617abd65b"      # YB/crvUSD (price_oracle)
DEFAULT_OUT = HERE / "crvusd_pools.csv.xz"
# Stableswap-only registries: get_virtual_price() · totalSupply() is a correct $ TVL
# (crypto/tricrypto registries use a different algorithm and are excluded).
REGISTRIES = ["main", "factory", "factory-stable-ng", "factory-crvusd"]

WAD = 10 ** 18
WORKERS = 16
REWARD_DATA_TYPES = ["address", "address", "uint256", "uint256", "uint256", "uint256"]
FEED_TYPES = ["uint80", "int256", "uint256", "uint256", "uint80"]


def sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def discover_pools(min_tvl=50_000.0):
    """Return (pool, gauge) pairs for STABLESWAP crvUSD/scrvUSD pools whose coins are
    ALL stablecoins (every coin priced ~$1) and with real TVL ≥ min_tvl. This keeps
    get_virtual_price·totalSupply a correct $ TVL and drops junk/volatile pools
    (e.g. tricrypto, or a POOH/wETH/crvUSD memecoin pool with lp_price ~ $88M)."""
    cl, sl = CRVUSD.lower(), SCRVUSD.lower()
    seen = {}
    for reg in REGISTRIES:
        r = requests.get(f"https://api.curve.finance/v1/getPools/ethereum/{reg}", timeout=60).json()
        if not r.get("success"):
            continue
        for p in r["data"]["poolData"]:
            coins = p.get("coins", [])
            addrs = [c.get("address", "").lower() for c in coins]
            g = p.get("gaugeAddress")
            prices = [float(c.get("usdPrice") or 0) for c in coins]
            if (cl in addrs or sl in addrs) and g and int(g, 16) != 0 \
                    and float(p.get("usdTotal", 0)) >= min_tvl \
                    and all(0.5 <= px <= 2.0 for px in prices):   # every coin is a stablecoin
                seen[p["address"]] = g
    return sorted(seen.items())


def blk_ts(rpc, b):
    return int(rpc.fetch("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16)


def block_for_timestamp(rpc, target, lo, hi):
    while lo < hi:
        mid = (lo + hi) // 2
        if blk_ts(rpc, mid) < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def parse_date(s):
    d = dt.datetime.fromisoformat(s)
    return int((d if d.tzinfo else d.replace(tzinfo=dt.UTC)).timestamp())


def build_calldata(gauged):
    """aggregate3 calldata over GAUGED all-stable crvUSD pools. Per pool: FULL staked
    TVL = gauge.totalSupply() · get_virtual_price() (all coins ~$1, so this is $),
    which excludes unstaked PegKeeper LP (never in the gauge). Plus CRV (rel_weight)
    and YB incentives."""
    calls = [
        (CRV, False, sel("rate()")),
        (CRV_USD_FEED, True, sel("latestRoundData()")),
        (YB_POOL, True, sel("price_oracle()")),
        (MC3, False, sel("getCurrentBlockTimestamp()")),
    ]
    for pool, g in gauged:
        calls.append((g, True, sel("totalSupply()")))                     # staked LP
        calls.append((pool, True, sel("get_virtual_price()")))            # $/LP (stableswap)
        calls.append((GAUGE_CONTROLLER, True,
                      sel("gauge_relative_weight(address)") + eth_abi.encode(["address"], [g])))
        calls.append((g, True, sel("reward_data(address)") + eth_abi.encode(["address"], [YB])))
    payload = eth_abi.encode(["(address,bool,bytes)[]"], [calls])
    return "0x" + (sel("aggregate3((address,bool,bytes)[])") + payload).hex()


def _u(b):
    return int.from_bytes(b, "big") if b else 0


def decode(result_hex, gauged):
    items = eth_abi.decode(["(bool,bytes)[]"], bytes.fromhex(result_hex[2:]))[0]
    crv_rate = _u(items[0][1]) / WAD
    crv_price = (eth_abi.decode(FEED_TYPES, items[1][1])[1] / 1e8) if items[1][0] and items[1][1] else 0.0
    yb_price = (_u(items[2][1]) / WAD) if items[2][0] else 0.0
    ts = _u(items[3][1])
    i = 4
    staked_tvl = sum_rw = yb_sum_rate = 0.0
    for pool, g in gauged:
        staked = _u(items[i][1]) / WAD if items[i][0] else 0.0
        vprice = _u(items[i + 1][1]) / WAD if items[i + 1][0] else 0.0
        staked_tvl += staked * vprice                      # full staked TVL ($)
        if items[i + 2][0] and items[i + 2][1]:
            sum_rw += _u(items[i + 2][1]) / WAD
        if items[i + 3][0] and items[i + 3][1]:
            rd = eth_abi.decode(REWARD_DATA_TYPES, items[i + 3][1])
            if ts < rd[2]:
                yb_sum_rate += rd[3] / WAD
        i += 4
    return (ts, staked_tvl, crv_rate, sum_rw, crv_price, yb_sum_rate, yb_price)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2025-10-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--points", type=int, default=1500)
    ap.add_argument("--batch", type=int, default=8, help="blocks per JSON-RPC POST")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    url = os.environ["NETWORK"]
    rpc = EthereumRPC(url)

    print("discovering crvUSD/scrvUSD pools …", flush=True)
    pairs = discover_pools()
    gauged = [(p, g) for p, g in pairs if g]      # only staked/incentivised pools matter
    print(f"  {len(pairs)} pools, {len(gauged)} gauged (used)")
    data = build_calldata(gauged)
    n_calls = 4 + 4 * len(gauged)
    print(f"  {n_calls} sub-calls per block")

    latest = int(rpc.fetch("eth_blockNumber", []), 16)
    start_block = block_for_timestamp(rpc, parse_date(args.start), 1, latest)
    end_block = (block_for_timestamp(rpc, parse_date(args.end), start_block, latest)
                 if args.end else latest)
    n = args.points
    span = end_block - start_block
    blocks = sorted({start_block + (span * k) // (n - 1) for k in range(n)})
    print(f"blocks {start_block}..{end_block} -> {len(blocks):,} samples", flush=True)

    tl = threading.local()
    chunks = [blocks[i:i + args.batch] for i in range(0, len(blocks), args.batch)]

    def fetch_chunk(blk_list):
        r = getattr(tl, "rpc", None)
        if r is None:
            r = tl.rpc = EthereumRPC(url)
        payloads = [("eth_call", [{"to": MC3, "data": data}, hex(b)]) for b in blk_list]
        for attempt in range(5):
            try:
                res = r.fetch_multi(payloads)
                return [(b, *decode(x, gauged)) for b, x in zip(blk_list, res)]
            except Exception as e:
                last = e
                time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"chunk at {blk_list[0]} failed: {last}")

    rows = []
    done = 0
    t0 = time.time()
    print(f"fetching {len(chunks)} chunks x {args.batch} blocks "
          f"({n_calls} calls/block, {args.workers} workers) …", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for out in ex.map(fetch_chunk, chunks):
            rows.extend(out)
            done += 1
            if done % 10 == 0 or done == len(chunks):
                reached = dt.datetime.fromtimestamp(out[-1][1], dt.UTC).date() if out else "?"
                rate = done / max(time.time() - t0, 1e-6)
                print(f"  {done:>4}/{len(chunks)} chunks  {len(rows):>5} rows  "
                      f"reached {reached}  ({rate:.1f} chunk/s)", flush=True)
    rows.sort()

    with lzma.open(args.out, "wt", newline="", preset=6) as fh:
        w = csv.writer(fh)
        w.writerow(["block_number", "timestamp", "datetime_utc", "staked_tvl",
                    "crv_rate", "sum_rel_weight", "crv_price", "yb_sum_rate", "yb_price",
                    "n_gauged"])
        for (b, ts, tvl, crate, srw, cpx, ybr, ybpx) in rows:
            w.writerow([b, ts, dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(),
                        tvl, crate, srw, cpx, ybr, ybpx, len(gauged)])
    print(f"wrote {len(rows):,} rows -> {args.out}")


if __name__ == "__main__":
    main()
