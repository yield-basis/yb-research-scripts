"""Fetch every capital-flow event of the YB markets (BTC v1/v2/v3 and WETH).

A "flow" is anything that changes an AMM's collateral/debt other than trading:
    AMM.AddLiquidityRaw     <- LT.deposit
    AMM.RemoveLiquidityRaw  <- LT.withdraw, LT.emergency_withdraw
The LT Deposit / Withdraw events are kept as well for their asset amounts
(BTC in / out), used for sanity checks, and LT WithdrawAdminFees marks where the
admin's accrued fee (liquidity.admin) is converted into LT shares and reset.

PnL is later measured only over block intervals that contain no flow of that
market, so this list defines which single blocks get excluded.

Output: data/events.csv.xz
    block, log_index, tx, market, version, contract, event, a0, a1
        AddLiquidityRaw:    a0 = LP collateral added,    a1 = crvUSD debt added
        RemoveLiquidityRaw: a0 = LP collateral removed,  a1 = crvUSD debt repaid
        Deposit / Withdraw: a0 = assets (asset wei),     a1 = LT shares
        WithdrawAdminFees:  a0 = LT shares minted to the fee receiver

Usage:
    uv run python fetch_events.py [--end-block N]
"""
from __future__ import annotations

import argparse
import csv
import lzma
from concurrent.futures import ThreadPoolExecutor

import eth_abi
from tqdm import tqdm

from yb_common import (DATA, LOGS_RANGE, RPC_BATCH, fetch_multi_retry, head_block, topic,
                       yb_markets)

EVENTS = {
    topic("AddLiquidityRaw(uint256[2],uint256,uint256)"): ("amm", "AddLiquidityRaw"),
    topic("RemoveLiquidityRaw(uint256,uint256)"): ("amm", "RemoveLiquidityRaw"),
    topic("Deposit(address,address,uint256,uint256)"): ("lt", "Deposit"),
    topic("Withdraw(address,address,address,uint256,uint256)"): ("lt", "Withdraw"),
    topic("WithdrawAdminFees(address,uint256)"): ("lt", "WithdrawAdminFees"),
}
WORKERS = 8


def decode(event: str, data: str) -> tuple[int, int]:
    raw = bytes.fromhex(data[2:])
    if event == "AddLiquidityRaw":
        amounts, _inv, _p_o = eth_abi.decode(["uint256[2]", "uint256", "uint256"], raw)
        return amounts[0], amounts[1]
    if event == "WithdrawAdminFees":
        _receiver, amount = eth_abi.decode(["address", "uint256"], raw)
        return amount, 0
    # RemoveLiquidityRaw(coll, debt), Deposit(assets, shares), Withdraw(assets, shares)
    return eth_abi.decode(["uint256", "uint256"], raw)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--end-block", type=int, default=None)
    args = ap.parse_args()

    markets = yb_markets()
    end = args.end_block or head_block()
    start = min(m.created_block for m in markets)
    by_addr = {}
    for m in markets:
        by_addr[m.amm.lower()] = m
        by_addr[m.lt.lower()] = m

    payloads = [("eth_getLogs", [{
        "address": sorted(by_addr),
        "fromBlock": hex(b), "toBlock": hex(min(b + LOGS_RANGE - 1, end)),
        "topics": [sorted(EVENTS)],
    }]) for b in range(start, end + 1, LOGS_RANGE)]
    batches = [payloads[i:i + RPC_BATCH // 4] for i in range(0, len(payloads), RPC_BATCH // 4)]

    rows = []
    with ThreadPoolExecutor(WORKERS) as ex, \
            tqdm(total=len(payloads), desc="getLogs", unit="chunk") as pbar:
        for results in ex.map(fetch_multi_retry, batches):
            for logs in results:
                for log in logs:
                    if log.get("removed"):
                        continue
                    m = by_addr[log["address"].lower()]
                    contract, event = EVENTS[log["topics"][0]]
                    if getattr(m, contract).lower() != log["address"].lower():
                        continue  # same topic emitted by the other contract type
                    a0, a1 = decode(event, log["data"])
                    rows.append((int(log["blockNumber"], 16), int(log["logIndex"], 16),
                                 log["transactionHash"], m.idx, m.version, contract, event, a0, a1))
            pbar.update(sum(1 for _ in results))

    rows.sort()
    DATA.mkdir(exist_ok=True)
    out = DATA / "events.csv.xz"
    with lzma.open(out, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["block", "log_index", "tx", "market", "version", "contract", "event", "a0", "a1"])
        w.writerows(rows)

    print(f"blocks {start}..{end}: {len(rows)} events -> {out}")
    for m in markets:
        n = [r for r in rows if r[3] == m.idx]
        flows = {r[0] for r in n if r[5] == "amm"}
        print(f"  [{m.idx}] {m.label:10s} {len(n):6d} events, {len(flows):6d} flow blocks")


if __name__ == "__main__":
    main()
