"""Per-event (≈ per-block) crvUSD net-pressure of the PYUSD/crvUSD sink pool.

Target pool (default): 0x625E92624Bc2D88619ACCc1788365A69767f6200
  Curve stableswap-ng PYUSD/crvUSD. coin0 = PYUSD (6 dec), coin1 = crvUSD (18 dec).

Why this pool = "net pressure": it is the crvUSD supply sink YB incentivises. When
the system runs a positive crvUSD net pressure (crvUSD shortfall / demand), crvUSD is
pulled OUT of this pool (arb sells PYUSD for the scarce crvUSD), so the pool skews
PYUSD-heavy. We therefore read net pressure as the per-half-TVL crvUSD depletion

    net_pressure = 2 * (b0_usd - b1_usd) / (b0_usd + b1_usd)      (+ = crvUSD scarce)

with b0_usd = balances(0)/1e6, b1_usd = balances(1)/1e18. This mirrors the factor-2,
per-half-TVL convention of net_pressure.py and is scale-invariant (a *balanced* LP
deposit/withdrawal leaves it unchanged — only directional flow moves it).

How the per-block series is built (cheaply, without a chain read per block)
---------------------------------------------------------------------------
The pool balances change ONLY on its events, so the balances are piecewise-constant
between events and reconstructing them from event amounts gives full per-event
(hence effectively per-block) resolution:

  * eth_getLogs (paginated in <=10k-block chunks, node limit) pulls every
    TokenExchange / AddLiquidity / RemoveLiquidity[One|Imbalance] event and we
    cumulative-sum the per-coin deltas onto a seed balance read once at the start.
  * Sparse ANCHORS (every --anchor-blocks, ~hours) read the *real*
    balances(0)/balances(1)/totalSupply + block timestamp via one Multicall3
    aggregate3. These do three jobs:
      1. timestamps for every event block are linearly interpolated between anchors
         (block time is ~12.06s and near-constant, so error is a few seconds — far
         below the controller's hourly cadence);
      2. reconciliation: reconstructed balances are compared to the on-chain truth
         (drift = tiny admin-fee leakage the events don't carry) and totalSupply to
         the event-reported supply (catches any MISSED event) — both printed;
      3. re-anchor: reconstructed balances are snapped to on-chain at every anchor so
         admin-fee drift can never accumulate.

Output (xz CSV, default pool_netpressure.csv.xz), one row per balance-changing event:
    block_number, timestamp, datetime_utc, balance0_raw, balance1_raw,
    b0_usd, b1_usd, tvl_usd, net_pressure, total_supply, event

Usage
-----
    uv run python fetch_pool_netpressure.py                       # 2025-10-01 -> head
    uv run python fetch_pool_netpressure.py --start 2025-06-01 --anchor-blocks 600
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
import numpy as np
from boa.rpc import EthereumRPC
from dotenv import load_dotenv
from eth_utils import keccak
from tqdm import tqdm

HERE = Path(__file__).resolve().parent

POOL = "0x625E92624Bc2D88619ACCc1788365A69767f6200"
MC3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
DECIMALS = (6, 18)                     # PYUSD, crvUSD
DEFAULT_OUT = HERE / "pool_netpressure.csv.xz"

LOGS_CHUNK = 10_000                    # node hard cap on eth_getLogs block range
BATCH = 100                           # node hard cap on JSON-RPC batch size
WORKERS = 24


def sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def topic0(sig: str) -> str:
    return "0x" + keccak(text=sig).hex()


# Balance-changing events -> (topic0, non-indexed abi types). Dispatched on topic0.
EV = {
    topic0("TokenExchange(address,int128,uint256,int128,uint256)"):
        ("TokenExchange", ["int128", "uint256", "int128", "uint256"]),
    topic0("AddLiquidity(address,uint256[],uint256[],uint256,uint256)"):
        ("AddLiquidity", ["uint256[]", "uint256[]", "uint256", "uint256"]),
    topic0("RemoveLiquidity(address,uint256[],uint256[],uint256)"):
        ("RemoveLiquidity", ["uint256[]", "uint256[]", "uint256"]),
    topic0("RemoveLiquidityOne(address,int128,uint256,uint256,uint256)"):
        ("RemoveLiquidityOne", ["int128", "uint256", "uint256", "uint256"]),
    topic0("RemoveLiquidityImbalance(address,uint256[],uint256[],uint256,uint256)"):
        ("RemoveLiquidityImbalance", ["uint256[]", "uint256[]", "uint256", "uint256"]),
}


def decode_event(t0: str, data_hex: str):
    """-> (name, db0, db1, reported_supply|None). Deltas are signed raw token units."""
    name, types = EV[t0]
    vals = eth_abi.decode(types, bytes.fromhex(data_hex[2:]))
    db0 = db1 = 0
    supply = None
    if name == "TokenExchange":
        sold_id, tokens_sold, bought_id, tokens_bought = vals
        d = {sold_id: tokens_sold, bought_id: -tokens_bought}
        db0, db1 = d.get(0, 0), d.get(1, 0)
    elif name in ("AddLiquidity", "RemoveLiquidityImbalance"):
        amts, _fees, _inv, supply = vals
        s = 1 if name == "AddLiquidity" else -1
        db0, db1 = s * amts[0], s * amts[1]
    elif name == "RemoveLiquidity":
        amts, _fees, supply = vals
        db0, db1 = -amts[0], -amts[1]
    elif name == "RemoveLiquidityOne":
        token_id, _lp_burned, coin_amount, supply = vals
        if token_id == 0:
            db0 = -coin_amount
        else:
            db1 = -coin_amount
    return name, db0, db1, supply


def anchor_calldata() -> str:
    """aggregate3([balances(0), balances(1), totalSupply, blockTimestamp])."""
    calls = [
        (POOL, False, sel("balances(uint256)") + eth_abi.encode(["uint256"], [0])),
        (POOL, False, sel("balances(uint256)") + eth_abi.encode(["uint256"], [1])),
        (POOL, False, sel("totalSupply()")),
        (MC3, False, sel("getCurrentBlockTimestamp()")),
    ]
    payload = eth_abi.encode(["(address,bool,bytes)[]"], [calls])
    return "0x" + (sel("aggregate3((address,bool,bytes)[])") + payload).hex()


def decode_anchor(result_hex: str) -> tuple[int, int, int, int]:
    items = eth_abi.decode(["(bool,bytes)[]"], bytes.fromhex(result_hex[2:]))[0]
    return tuple(int.from_bytes(items[i][1], "big") for i in range(4))  # b0,b1,supply,ts


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
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", default=POOL)
    ap.add_argument("--start", default="2025-10-01", help="start date (UTC ISO)")
    ap.add_argument("--end", default=None, help="end date (UTC ISO); default chain head")
    ap.add_argument("--anchor-blocks", type=int, default=900,
                    help="blocks between reconcile/timestamp anchors (~12.06s/blk; 900~=3h)")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    url = os.environ.get("NETWORK")
    if not url:
        raise SystemExit("NETWORK not set (see .env / .env.example)")
    rpc = EthereumRPC(url)
    tl = threading.local()

    def rpc_local() -> EthereumRPC:
        r = getattr(tl, "rpc", None)
        if r is None:
            r = tl.rpc = EthereumRPC(url)
        return r

    latest = int(rpc.fetch("eth_blockNumber", []), 16)
    start_block = block_for_timestamp(rpc, parse_date(args.start), 1, latest)
    end_block = latest if args.end is None else block_for_timestamp(
        rpc, parse_date(args.end), start_block, latest)
    print(f"pool {args.pool}")
    print(f"blocks {start_block:,} .. {end_block:,}  "
          f"({dt.datetime.fromtimestamp(blk_ts(rpc, start_block), dt.UTC):%Y-%m-%d} .. "
          f"{dt.datetime.fromtimestamp(blk_ts(rpc, end_block), dt.UTC):%Y-%m-%d})")

    # --- seed balances at (start_block-1): state just before the first counted event
    seed_hex = rpc.fetch("eth_call", [{"to": MC3, "data": anchor_calldata()},
                                      hex(start_block - 1)])
    b0, b1, sup_chain, _ = decode_anchor(seed_hex)
    print(f"seed @ {start_block-1:,}: b0={b0/1e6:,.0f} PYUSD  b1={b1/1e18:,.0f} crvUSD  "
          f"supply={sup_chain/1e18:,.0f}")

    # --- anchors: (block, b0, b1, supply, ts) on a fixed grid, batched multicall
    anchors = list(range(start_block, end_block + 1, args.anchor_blocks))
    if anchors[-1] != end_block:
        anchors.append(end_block)
    data = anchor_calldata()

    def fetch_anchor_batch(blks):
        r = rpc_local()
        payloads = [("eth_call", [{"to": MC3, "data": data}, hex(b)]) for b in blks]
        for attempt in range(5):
            try:
                res = r.fetch_multi(payloads)
                return [(b, *decode_anchor(x)) for b, x in zip(blks, res)]
            except Exception as e:
                last = e
                time.sleep(0.3 * (attempt + 1))
        raise RuntimeError(f"anchor batch @ {blks[0]} failed: {last}")

    achunks = [anchors[i:i + BATCH] for i in range(0, len(anchors), BATCH)]
    anchor_rows = []
    with tqdm(total=len(anchors), desc="anchors", unit="blk", dynamic_ncols=True) as pb:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for rows in ex.map(fetch_anchor_batch, achunks):
                anchor_rows.extend(rows)
                pb.update(len(rows))
    anchor_rows.sort()
    a_blk = np.array([r[0] for r in anchor_rows], dtype=np.int64)
    a_b0 = np.array([r[1] for r in anchor_rows], dtype=object)
    a_b1 = np.array([r[2] for r in anchor_rows], dtype=object)
    a_sup = np.array([r[3] for r in anchor_rows], dtype=object)
    a_ts = np.array([r[4] for r in anchor_rows], dtype=np.int64)

    # --- events: parallel getLogs over 10k-block chunks
    topics = [list(EV.keys())]
    ranges = [(a, min(a + LOGS_CHUNK - 1, end_block))
              for a in range(start_block, end_block + 1, LOGS_CHUNK)]

    def fetch_logs(rng):
        a, b = rng
        r = rpc_local()
        flt = {"address": args.pool, "fromBlock": hex(a), "toBlock": hex(b), "topics": topics}
        for attempt in range(5):
            try:
                return r.fetch("eth_getLogs", [flt])
            except Exception as e:
                last = e
                time.sleep(0.3 * (attempt + 1))
        raise RuntimeError(f"getLogs {a}-{b} failed: {last}")

    all_logs = []
    with tqdm(total=len(ranges), desc="getLogs", unit="chunk", dynamic_ncols=True) as pb:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for logs in ex.map(fetch_logs, ranges):
                all_logs.extend(logs)
                pb.update(1)
    # order by (block, logIndex)
    all_logs.sort(key=lambda l: (int(l["blockNumber"], 16), int(l["logIndex"], 16)))
    print(f"{len(all_logs):,} balance-changing events")

    # interpolate a timestamp for any block from the anchor (block -> ts) map
    def ts_of(block: int) -> int:
        return int(round(float(np.interp(block, a_blk, a_ts))))

    # --- walk events, cumulative-sum balances, re-anchor at each anchor grid point
    s0, s1 = 10 ** DECIMALS[0], 10 ** DECIMALS[1]
    sup = sup_chain
    ai = 0                                    # next anchor index to reconcile at
    drift0, drift1, dsup, n_anchor_chk = [], [], [], 0
    out_rows = []

    def reconcile_upto(block: int):
        nonlocal ai, b0, b1, sup, n_anchor_chk
        while ai < len(a_blk) and a_blk[ai] <= block:
            # all events with block <= a_blk[ai] are already applied
            cb0, cb1, csup = int(a_b0[ai]), int(a_b1[ai]), int(a_sup[ai])
            drift0.append((b0 - cb0) / s0)
            drift1.append((b1 - cb1) / s1)
            dsup.append((sup - csup) / s1)
            n_anchor_chk += 1
            b0, b1, sup = cb0, cb1, csup      # snap
            ai += 1

    for l in all_logs:
        blk = int(l["blockNumber"], 16)
        reconcile_upto(blk - 1)               # settle anchors strictly before this block
        name, db0, db1, rsup = decode_event(l["topics"][0], l["data"])
        b0 += db0
        b1 += db1
        if rsup is not None:
            sup = rsup                        # events carry the exact resulting supply
        u0, u1 = b0 / s0, b1 / s1
        tvl = u0 + u1
        npr = 2.0 * (u0 - u1) / tvl if tvl > 0 else 0.0
        ts = ts_of(blk)
        out_rows.append((blk, ts, dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(),
                         b0, b1, u0, u1, tvl, npr, sup / s1, name))
    reconcile_upto(end_block)

    # --- reconciliation report
    d0, d1, ds = np.array(drift0), np.array(drift1), np.array(dsup)
    print(f"\nreconciliation over {n_anchor_chk} anchors (reconstructed - on-chain, "
          f"pre-snap):")
    print(f"  balance0 (PYUSD)  drift: median {np.median(d0):+.2f}  p95 {np.percentile(np.abs(d0),95):.2f}  max|{np.abs(d0).max():.2f}|  $")
    print(f"  balance1 (crvUSD) drift: median {np.median(d1):+.2f}  p95 {np.percentile(np.abs(d1),95):.2f}  max|{np.abs(d1).max():.2f}|  $")
    print(f"  totalSupply       drift: max|{np.abs(ds).max():.4f}|  (should be ~0 -> no missed events)")

    with lzma.open(args.out, "wt", newline="", preset=6) as fh:
        w = csv.writer(fh)
        w.writerow(["block_number", "timestamp", "datetime_utc", "balance0_raw",
                    "balance1_raw", "b0_usd", "b1_usd", "tvl_usd", "net_pressure",
                    "total_supply", "event"])
        for r in out_rows:
            w.writerow(r)
    print(f"\nwrote {len(out_rows):,} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
