#!/usr/bin/env python3
"""Find where YB tokens were streamed by aggregating Transfer recipients on-chain.

Used to locate the StakeDAO reward distributor for the pyUSD/crvUSD pool — the
Votemarket-LM "direct incentive" YB stream that the Curve-gauge reward_data misses.

Reads YB Transfer logs over a block window via the project node (NETWORK in .env),
batching eth_getLogs with fetch_multi (pnl-style) and a tqdm progress bar, then
prints the largest net YB recipients.

Usage
-----
    uv run python trace_yb_recipients.py --from 24882395 --to 25100000
"""
import argparse
import os
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from boa.rpc import EthereumRPC
from tqdm import tqdm

HERE = Path(__file__).resolve().parent
YB = "0x01791F726B4103694969820be083196cC7c045fF"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CHUNK = 1000      # blocks per eth_getLogs
BATCH = 25        # getLogs calls per JSON-RPC POST


def fetch_transfers(rpc, token, lo, hi):
    chunks = [(b, min(b + CHUNK - 1, hi)) for b in range(lo, hi + 1, CHUNK)]
    logs_all = []
    for i in tqdm(range(0, len(chunks), BATCH), desc="getLogs", unit="batch"):
        grp = chunks[i:i + BATCH]
        payloads = [("eth_getLogs", [{"address": token, "topics": [TRANSFER],
                                       "fromBlock": hex(a), "toBlock": hex(z)}])
                    for a, z in grp]
        for logs in rpc.fetch_multi(payloads):
            logs_all.extend(logs)
    return logs_all


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="lo", type=int, required=True)
    ap.add_argument("--to", dest="hi", type=int, required=True)
    ap.add_argument("--token", default=YB)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    rpc = EthereumRPC(os.environ["NETWORK"])
    logs = fetch_transfers(rpc, args.token, args.lo, args.hi)

    recv = defaultdict(float)
    sent = defaultdict(float)
    cnt = defaultdict(int)
    for L in logs:
        frm = "0x" + L["topics"][1][-40:]
        to = "0x" + L["topics"][2][-40:]
        v = int(L["data"], 16) / 1e18
        recv[to] += v
        sent[frm] += v
        cnt[to] += 1
    total = sum(recv.values())
    print(f"\n{len(logs):,} transfers, {len(recv)} recipients, total {total:,.0f} YB")
    print("top net recipients (received − sent):")
    net = {a: recv[a] - sent.get(a, 0.0) for a in recv}
    for addr, v in sorted(net.items(), key=lambda kv: -kv[1])[:args.top]:
        print(f"  {addr}  net {v:14,.1f}  (recv {recv[addr]:,.0f} in {cnt[addr]} xfers)")


if __name__ == "__main__":
    main()
