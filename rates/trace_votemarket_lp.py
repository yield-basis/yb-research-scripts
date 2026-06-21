#!/usr/bin/env python3
"""Split a Votemarket YB campaign into voter (veCRV) vs LP shares.

Method (per the campaign mechanics): the campaign pays in pYB (bridged YB) on
Arbitrum; claimers receive pYB and bridge it to mainnet. An address that claims is
a **voter** if it directed veCRV voting power to the target gauge (Curve
GaugeController) — those YB went to vote-buying, NOT to LPs. Any claimer with **no**
veCRV vote for the gauge is the LP-incentive path (the IncentiveGaugeHook /
distributor). So:

  LP incentive  = sum of pYB claimed by addresses with no gauge vote
  voter rewards = sum claimed by addresses that voted for the gauge

Reads pYB Transfer-out-of-platform on Arbitrum (claims), then checks each claimer's
`vote_user_slopes(user, gauge)` on mainnet.

Usage
-----
    uv run python trace_votemarket_lp.py --from-ts 2026-04-16 --to-ts 2026-06-21
"""
import argparse
import datetime as dt
import os
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from boa.rpc import EthereumRPC
from eth_utils import keccak
import eth_abi
from tqdm import tqdm

HERE = Path(__file__).resolve().parent
PLATFORM = "0x8c2c5A295450DDFf4CB360cA73FCCC12243D14D9"
PYB = "0x504acfa597dcdf3cfeeb63149a967a3e863a5ee0"          # pYB on Arbitrum
GAUGE = "0xf69Fb60B79E463384b40dbFDFB633AB5a863C9A2"
GAUGE_CONTROLLER = "0x2F50D538606Fa9EDD2B11E2446BEb18C9D5846bB"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CHUNK = 50_000
BATCH = 20


def sel(s):
    return keccak(text=s)[:4]


def ts_to_block(rpc, target):
    lo, hi = 1, int(rpc.fetch("eth_blockNumber", []), 16)
    def bts(b):
        return int(rpc.fetch("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16)
    while lo < hi:
        mid = (lo + hi) // 2
        if bts(mid) < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def topic_addr(a):
    return "0x" + "0" * 24 + a[2:].lower()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-ts", required=True)
    ap.add_argument("--to-ts", required=True)
    args = ap.parse_args()
    load_dotenv(HERE / ".env")
    arb = EthereumRPC(os.environ["ARBITRUM_RPC"])
    eth = EthereumRPC(os.environ["NETWORK"])

    lo_t = int(dt.datetime.fromisoformat(args.from_ts).replace(tzinfo=dt.UTC).timestamp())
    hi_t = int(dt.datetime.fromisoformat(args.to_ts).replace(tzinfo=dt.UTC).timestamp())
    lo, hi = ts_to_block(arb, lo_t), ts_to_block(arb, hi_t)
    print(f"arbitrum blocks {lo:,} .. {hi:,}")

    # pYB transfers OUT of the platform (claims) — from-filtered
    chunks = [(b, min(b + CHUNK - 1, hi)) for b in range(lo, hi + 1, CHUNK)]
    claimed = defaultdict(float)
    for i in tqdm(range(0, len(chunks), BATCH), desc="claims", unit="batch"):
        grp = chunks[i:i + BATCH]
        payloads = [("eth_getLogs", [{"address": PYB, "topics": [TRANSFER, topic_addr(PLATFORM)],
                                      "fromBlock": hex(a), "toBlock": hex(z)}]) for a, z in grp]
        for logs in arb.fetch_multi(payloads):
            for L in logs:
                to = "0x" + L["topics"][2][-40:]
                claimed[to] += int(L["data"], 16) / 1e18
    print(f"{len(claimed)} claim recipients, total {sum(claimed.values()):,.0f} pYB")

    # classify each recipient by gauge vote on mainnet
    def voted(user):
        d = eth.fetch("eth_call", [{"to": GAUGE_CONTROLLER,
              "data": "0x" + (sel("vote_user_slopes(address,address)")
              + eth_abi.encode(["address", "address"], [user, GAUGE])).hex()}, "latest"])
        slope, power, end = eth_abi.decode(["uint256", "uint256", "uint256"], bytes.fromhex(d[2:]))
        return power
    rows = []
    for addr, amt in tqdm(sorted(claimed.items(), key=lambda kv: -kv[1]),
                          desc="votes", unit="addr"):
        rows.append((addr, amt, voted(addr)))
    lp = sum(a for _, a, p in rows if p == 0)
    voter = sum(a for _, a, p in rows if p > 0)
    print(f"\n  LP incentive (no gauge vote): {lp:,.0f} pYB")
    print(f"  voter rewards (voted gauge)  : {voter:,.0f} pYB")
    print("\n  per recipient (amount, gauge vote power bps):")
    for addr, amt, p in rows[:25]:
        print(f"    {addr}  {amt:12,.0f}  power={p}  -> {'VOTER' if p > 0 else 'LP'}")


if __name__ == "__main__":
    main()
