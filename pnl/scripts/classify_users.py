"""Classify each user in pnl_all_users.csv by address type at a given block.

Categories:
  - EOA            : no code at the address
  - HybridVault    : answers to required_crvusd() (YB-HybridVault-specific view)
  - Safe           : answers to BOTH getThreshold() and VERSION() (Gnosis Safe)
  - Other          : has code but didn't match the above

For contracts, we also scan the bytecode for PUSH4-prefixed selectors of
common ERC20-rescue functions (rescueERC20, sweep, recoverERC20,
inCaseTokensGetStuck, etc.). The result populates `has_rescue`. Contracts
with `addr_type=Other AND has_rescue=False` are stuck-prone — any token
sent to them (e.g. YB rewards) cannot be extracted.

Output: pnl_all_users_classified.csv (input + addr_type, has_rescue cols)

Usage:
    uv run python scripts/classify_users.py [CSV_PATH] [--block BLOCK]
"""
from __future__ import annotations

import os
import sys
import time as _time

import polars as pl
import requests
from dotenv import load_dotenv
from eth_abi import decode as abi_decode, encode as abi_encode
from tqdm import tqdm
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from yb import rpc_url, w3  # noqa: E402

load_dotenv()


MULTICALL3 = Web3.to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")
SEL_GETTHRESHOLD = Web3.keccak(text="getThreshold()")[:4]
SEL_VERSION = Web3.keccak(text="VERSION()")[:4]
SEL_REQUIRED_CRVUSD = Web3.keccak(text="required_crvusd()")[:4]
SEL_AGGREGATE3 = Web3.keccak(text="aggregate3((address,bool,bytes)[])")[:4]

# Common selectors that let an admin pull arbitrary ERC20 tokens out of a
# contract. Presence in bytecode (as PUSH4 immediate) means SOMEONE can
# rescue stuck tokens; absence means stuck-forever.
RESCUE_SIGS = [
    "rescueERC20(address,uint256)",
    "rescueERC20(address,address,uint256)",
    "rescue(address,uint256)",
    "rescue(address,address,uint256)",
    "rescueToken(address,uint256)",
    "rescueToken(address,address,uint256)",
    "sweep(address)",
    "sweep(address,uint256)",
    "sweep(address,address,uint256)",
    "sweepToken(address,address,uint256)",
    "recoverERC20(address,uint256)",
    "recoverERC20(address,address,uint256)",
    "recoverToken(address,uint256)",
    "recoverToken(address,address,uint256)",
    "recover(address,uint256)",
    "recover(address,address,uint256)",
    "recover_tokens(address)",            # YB HybridVault
    "withdrawERC20(address,uint256)",
    "withdrawERC20(address,address,uint256)",
    "withdrawToken(address,uint256)",
    "withdrawToken(address,address,uint256)",
    "transferToken(address,address,uint256)",
    "inCaseTokensGetStuck(address)",      # Yearn
    "execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)",  # Safe
    "execute(address,uint256,bytes)",     # generic smart wallets / 4337
    "executeBatch(address[],uint256[],bytes[])",  # 4337 batched
]
RESCUE_SELECTORS = [Web3.keccak(text=s)[:4] for s in RESCUE_SIGS]

# Bytecode size below which we assume the contract is a proxy (minimal /
# Safe / OZ transparent / 4337 — all ≤ ~250 bytes). Selectors live in
# the impl, not the proxy, so a bytecode scan is meaningless here.
PROXY_BYTECODE_BYTES = 250


def is_proxy_likely(bytecode_hex: str) -> bool:
    if not bytecode_hex or bytecode_hex == "0x":
        return False
    return (len(bytecode_hex) - 2) // 2 < PROXY_BYTECODE_BYTES


def has_rescue(bytecode_hex: str) -> bool:
    """Look for `PUSH4 <selector>` (0x63 + 4 bytes) of any rescue selector.

    Solidity dispatchers always reference function selectors via PUSH4, so
    the prefix prevents false positives from selectors appearing in
    arbitrary immediates.

    NB: returns False for proxy contracts (their bytecode just delegates;
    selectors live in the implementation). Use is_proxy_likely() to gate.
    """
    if not bytecode_hex or bytecode_hex == "0x":
        return False
    code = bytes.fromhex(bytecode_hex[2:])
    for sel in RESCUE_SELECTORS:
        if b"\x63" + sel in code:
            return True
    return False

GETCODE_BATCH = 100      # JSON-RPC eth_getCode batch
CONTRACTS_PER_MC = 50    # contracts per Multicall3 (each contributes 3 sub-calls)


def _retry(fn, label: str, retries: int = 6):
    last = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            last = e
            wait = min(2 ** attempt, 30)
            print(f"  retry {label}: {type(e).__name__} attempt {attempt + 1}/{retries}; sleep {wait}s")
            _time.sleep(wait)
    raise last  # type: ignore[misc]


def aggregate3_calldata(calls):
    """calls: list of (address-checksum, bool allowFailure, bytes callData)."""
    payload = abi_encode(["(address,bool,bytes)[]"], [calls])
    return "0x" + SEL_AGGREGATE3.hex() + payload.hex()


def main() -> None:
    args = sys.argv[1:]
    block_override = None
    while "--block" in args:
        i = args.index("--block")
        block_override = int(args[i + 1])
        args = args[:i] + args[i + 2:]
    csv_path = args[0] if args else "pnl_all_users.csv"

    client = w3()
    block = block_override if block_override else client.eth.block_number

    df = pl.read_csv(csv_path)
    addrs = sorted(df["user"].unique().to_list())
    addrs_cs = [Web3.to_checksum_address(a) for a in addrs]
    print(f"{len(addrs_cs)} unique addresses; block={block}")

    url = rpc_url()
    sess = requests.Session()

    # ---- Phase 1: eth_getCode batched ----
    code: dict[str, str] = {}
    pbar = tqdm(total=len(addrs_cs), desc="eth_getCode", unit="addr")
    for i in range(0, len(addrs_cs), GETCODE_BATCH):
        chunk = addrs_cs[i:i + GETCODE_BATCH]
        payload = [{"jsonrpc": "2.0", "id": j, "method": "eth_getCode",
                    "params": [a, hex(block)]}
                   for j, a in enumerate(chunk)]
        resp = _retry(lambda: sess.post(url, json=payload, timeout=120),
                      label=f"getCode {i}")
        resp.raise_for_status()
        results = resp.json()
        if not isinstance(results, list):
            raise RuntimeError(f"non-list response: {results}")
        by_id = {r["id"]: r for r in results}
        for j, a in enumerate(chunk):
            r = by_id[j]
            if "result" not in r:
                raise RuntimeError(f"eth_getCode error {a}: {r.get('error')}")
            code[a] = r["result"]
        pbar.update(len(chunk))
    pbar.close()

    contracts = [a for a, c in code.items() if c != "0x"]
    eoas = [a for a, c in code.items() if c == "0x"]
    print(f"  → {len(eoas)} EOAs, {len(contracts)} contracts")

    # ---- Phase 2: Multicall3 probes for contracts ----
    classification: dict[str, str] = {a: "EOA" for a in eoas}

    pbar = tqdm(total=len(contracts), desc="probe contracts", unit="addr")
    for i in range(0, len(contracts), CONTRACTS_PER_MC):
        cset = contracts[i:i + CONTRACTS_PER_MC]
        calls = []
        for c in cset:
            calls.append((c, True, SEL_GETTHRESHOLD))
            calls.append((c, True, SEL_VERSION))
            calls.append((c, True, SEL_REQUIRED_CRVUSD))
        cd = aggregate3_calldata(calls)
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                   "params": [{"to": MULTICALL3, "data": cd}, hex(block)]}
        resp = _retry(lambda: sess.post(url, json=payload, timeout=120),
                      label=f"mc3 batch {i}")
        resp.raise_for_status()
        body = resp.json()
        if "result" not in body:
            raise RuntimeError(f"multicall error: {body.get('error')}")
        ret_bytes = bytes.fromhex(body["result"][2:])
        decoded = abi_decode(["(bool,bytes)[]"], ret_bytes)[0]

        for j, c in enumerate(cset):
            t_ok, _ = decoded[j * 3 + 0]
            v_ok, _ = decoded[j * 3 + 1]
            r_ok, _ = decoded[j * 3 + 2]
            if r_ok:
                classification[c] = "HybridVault"
            elif t_ok and v_ok:
                classification[c] = "Safe"
            else:
                classification[c] = "Other"
        pbar.update(len(cset))
    pbar.close()

    # ---- Phase 3: bytecode scan for rescue capability + proxy detection ----
    rescue_map: dict[str, bool] = {}
    proxy_map: dict[str, bool] = {}
    for a in addrs_cs:
        c = code[a]
        proxy_map[a] = is_proxy_likely(c)
        if c == "0x":
            rescue_map[a] = False  # EOA — N/A
        elif classification[a] in ("Safe", "HybridVault"):
            # Both are proxy-deployed (Safe → singleton with execTransaction;
            # YB HybridVault → impl with recover_tokens(IERC20)). Hardcode
            # rescue=True since we already verified the type via interface
            # probe (getThreshold/VERSION or required_crvusd).
            rescue_map[a] = True
        else:
            rescue_map[a] = has_rescue(c)

    # ---- Join + summarize + save ----
    cls_df = pl.DataFrame({
        "user_cs": list(classification.keys()),
        "addr_type": list(classification.values()),
        "has_rescue": [rescue_map[a] for a in classification.keys()],
        "is_proxy_likely": [proxy_map[a] for a in classification.keys()],
    })
    cls_df = cls_df.with_columns(user=pl.col("user_cs").str.to_lowercase()).drop("user_cs")
    out = df.join(cls_df, on="user", how="left")
    out_csv = csv_path.replace(".csv", "_classified.csv")
    if out_csv == csv_path:
        out_csv = csv_path + ".classified"
    out.write_csv(out_csv)
    print(f"\nSaved {out_csv}")

    # Don't truncate addresses in the printed tables — they're 42 chars.
    pl.Config.set_fmt_str_lengths(50)
    pl.Config.set_tbl_rows(50)

    print("\nUnique addresses by type:")
    type_summary = (
        cls_df.group_by("addr_type")
        .agg(pl.len().alias("addresses"))
        .sort("addresses", descending=True)
    )
    print(type_summary)

    print("\nContracts split by rescue capability:")
    rescue_summary = (
        cls_df.filter(pl.col("addr_type") != "EOA")
        .group_by(["addr_type", "has_rescue"])
        .agg(pl.len().alias("addresses"))
        .sort(["addr_type", "has_rescue"])
    )
    print(rescue_summary)

    print("\n⚠ Stuck-prone contracts (Other, no rescue, AND not a proxy):")
    print("  Excludes proxies because their selectors live in the impl, "
          "which we haven't followed — could be smart-wallet-class.")
    stuck = (
        out.filter(
            (pl.col("addr_type") == "Other")
            & ~pl.col("has_rescue")
            & ~pl.col("is_proxy_likely")
        )
        .group_by("user")
        .agg(pl.col("avg_pos").sum().alias("Σ avg_pos (mixed asset units)"))
        .sort("Σ avg_pos (mixed asset units)", descending=True)
    )
    print(f"  {len(stuck)} addresses, top 10 by Σ avg_pos:")
    print(stuck.head(10))

    print("\n  Other contracts that ARE proxies (impl unknown — could be wallets):")
    proxies = (
        out.filter((pl.col("addr_type") == "Other") & pl.col("is_proxy_likely"))
        .group_by("user")
        .agg(pl.col("avg_pos").sum().alias("Σ avg_pos (mixed asset units)"))
        .sort("Σ avg_pos (mixed asset units)", descending=True)
    )
    print(f"  {len(proxies)} addresses, top 5 by Σ avg_pos:")
    print(proxies.head(5))

    print("\nRows in CSV (one per market) by type, summed avg_pos in native asset units:")
    row_summary = (
        out.group_by("addr_type")
        .agg([
            pl.len().alias("rows"),
            pl.col("user").n_unique().alias("addresses"),
            pl.col("avg_pos").sum().alias("Σ avg_pos (mixed asset units)"),
        ])
        .sort("rows", descending=True)
    )
    print(row_summary)


if __name__ == "__main__":
    main()
