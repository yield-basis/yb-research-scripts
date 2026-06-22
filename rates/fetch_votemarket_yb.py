"""Fetch all Votemarket pYB (StakeDAO YB) campaigns to crvUSD-pool gauges.

These are the StakeDAO/Votemarket Liquidity-Mining YB incentives that do NOT show
up in the Curve gauge reward_data (and are NOT Merkl). Reads the Votemarket v2
Curve platform on Arbitrum; the reward token is pYB (bridged YB).

Output: votemarket_yb_campaigns.csv — campaign_id, gauge, total_yb, n_periods,
voting_start, voting_end (LP rewards land one week after the voting window).

Usage: uv run python fetch_votemarket_yb.py
"""
import csv
import datetime as dt
import os
from pathlib import Path

from dotenv import load_dotenv
from boa.rpc import EthereumRPC
from eth_utils import keccak
import eth_abi

HERE = Path(__file__).resolve().parent
PLATFORM = "0x8c2c5A295450DDFf4CB360cA73FCCC12243D14D9"
PYB = "0x504acfa597dcdf3cfeeb63149a967a3e863a5ee0"
OUT = HERE / "votemarket_yb_campaigns.csv"
CT = ["uint256", "address", "address", "address", "uint8", "uint256", "uint256",
      "uint256", "uint256", "uint256", "address"]


def main():
    load_dotenv(HERE / ".env")
    rpc = EthereumRPC(os.environ["ARBITRUM_RPC"])
    sel = lambda s: keccak(text=s)[:4]
    n = int(rpc.fetch("eth_call", [{"to": PLATFORM, "data": "0x" + sel("campaignCount()").hex()}, "latest"]), 16)
    rows = []
    for i0 in range(0, n, 50):
        ids = list(range(i0, min(i0 + 50, n)))
        res = rpc.fetch_multi([("eth_call", [{"to": PLATFORM, "data": "0x" + (
            sel("campaignById(uint256)") + eth_abi.encode(["uint256"], [i])).hex()}, "latest"]) for i in ids])
        for i, r in zip(ids, res):
            c = eth_abi.decode(CT, bytes.fromhex(r[2:]))
            if c[3].lower() == PYB.lower():
                # hook (c[10]): 0x0 = plain Votemarket bribe paid to VOTERS; a non-zero
                # IncentiveGaugeHook routes the YB to the pool's LPs instead. Only the
                # latter is a direct LP incentive (the former's effect is already in CRV).
                rows.append((i, c[1], c[6] / 1e18, c[4], c[8], c[9], c[10]))
    rows.sort(key=lambda x: x[4])
    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["campaign_id", "gauge", "total_yb", "n_periods", "voting_start", "voting_end", "hook"])
        for cid, gauge, tot, nper, s0, e0, hook in rows:
            w.writerow([cid, gauge, f"{tot:.2f}", nper,
                        dt.datetime.fromtimestamp(s0, dt.UTC).date(),
                        dt.datetime.fromtimestamp(e0, dt.UTC).date(), hook])
    print(f"wrote {len(rows)} pYB campaigns -> {OUT}")
    print(f"total YB across campaigns: {sum(r[2] for r in rows):,.0f}")


if __name__ == "__main__":
    main()
