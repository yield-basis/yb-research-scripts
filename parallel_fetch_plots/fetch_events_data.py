#!/usr/bin/env python3
"""
/fetch_events_data.py

One-shot scripty fetcher that:
- Scans events for TwoCrypto (AddLiquidity, RemoveLiquidity, TokenExchange) and LT (Deposit, Withdraw)
- Writes per-pool events to /data/<key>/events.csv
- Builds a unique set of boundary blocks (including start/end and event blocks)
- Fetches states for cp/lt/amm at those blocks via Multicall
- Writes per-pool states to /data/<key>/states.csv

Config is hardcoded below. Just run: python /fetch_events_data.py

Notes
- Requires WEB3_PROVIDER_URL (or ETH_RPC_URL) and ETHERSCAN_API_KEY in your environment.
- Designed for mainnet, using Etherscan ABI fetch.
- Test window defaults to start_block=23_434_000, end_block=23_440_000.
"""

import csv
import json
import os
from pathlib import Path

from web3 import Web3
from web3._utils.events import event_abi_to_log_topic
from web3mc import Multicall
import pandas as pd
import pandas as pd

# ---------------- Config ----------------

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data"
ABI_DIR = ROOT / "abi"

WEB3_URL = os.environ.get("WEB3_PROVIDER_URL") or os.environ.get("ETH_RPC_URL") or os.environ.get("WEB3_PROVIDER_URI") or ""
ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY") or os.environ.get("ETHERSCAN_TOKEN") or ""

# test window
START_BLOCK = 23_435_000
END_BLOCK = None

# Factory (used to derive LT/AMM if needed)
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"

# Pools are discovered via factory market IDs
# Edit this list to select factory indices to scan
POOL_IDS = [0, 1, 2]


# ---------------- ABI helpers ----------------

def ensure_dirs(pool_key: str):
    (DATA_ROOT / pool_key).mkdir(parents=True, exist_ok=True)
    ABI_DIR.mkdir(parents=True, exist_ok=True)


def fetch_abi(address: str):
    addr = Web3.to_checksum_address(address)
    cache = ABI_DIR / f"{addr}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    if not ETHERSCAN_API_KEY:
        raise SystemExit("Set ETHERSCAN_API_KEY to fetch ABIs")
    url = f"https://api.etherscan.io/v2/api?chainid=1&module=contract&action=getabi&address={addr}&apikey={ETHERSCAN_API_KEY}"
    import urllib.request
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read().decode())
    if data.get("status") != "1":
        raise RuntimeError(f"Etherscan ABI fetch failed for {addr}: {data}")
    abi = json.loads(data["result"])
    cache.write_text(json.dumps(abi, indent=2))
    return abi


def topic_map_for(abi: list, include_names: list[str]):
    names = {n.lower() for n in include_names}
    out = {}
    for e in abi:
        if e.get("type") != "event":
            continue
        n = e.get("name", "")
        if n.lower() in names:
            try:
                out[event_abi_to_log_topic(e)] = n
            except Exception:
                pass
    return out  # {topic: name}


# ---------------- Factory discovery ----------------

def discover_market_by_id(W3: Web3, market_id: int):
    """Return (cp_addr, lt_addr, amm_addr) for a given factory market index."""
    factory_abi = fetch_abi(FACTORY)
    factory = W3.eth.contract(address=Web3.to_checksum_address(FACTORY), abi=factory_abi)
    try:
        m = factory.functions.markets(market_id).call()
    except Exception as e:
        raise SystemExit(f"factory.markets({market_id}) failed: {e}")
    # Expected layout: m[2]=amm, m[3]=lt (as in v3)
    if not (isinstance(m, (list, tuple)) and len(m) >= 4):
        raise SystemExit(f"Unexpected markets({market_id}) layout: {m}")
    amm_addr = Web3.to_checksum_address(m[2])
    lt_addr = Web3.to_checksum_address(m[3])
    # derive cp via amm.COLLATERAL()
    amm_abi = fetch_abi(amm_addr)
    amm = W3.eth.contract(address=amm_addr, abi=amm_abi)
    cp_addr = Web3.to_checksum_address(amm.functions.COLLATERAL().call())
    return cp_addr, lt_addr, amm_addr


# ---------------- Events scanning ----------------

def scan_events(W3: Web3, addr: str, abi: list, event_names: list[str], start: int, end: int, batch: int = 1000):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    tmap = topic_map_for(abi, event_names)
    topics = list(tmap.keys())
    start = int(start)
    end = int(end) if end is not None else int(W3.eth.block_number)

    ranges = []
    cur = start
    while cur <= end:
        to = min(cur + batch - 1, end)
        ranges.append((cur, to))
        cur = to + 1

    def fetch_range(fr, to):
        params = {"address": Web3.to_checksum_address(addr), "fromBlock": fr, "toBlock": to}
        if topics:
            params["topics"] = [topics]
        try:
            logs = W3.eth.get_logs(params)
        except Exception:
            logs = W3.eth.get_logs({k: v for k, v in params.items() if k != "topics"})
        out = []
        for lg in logs:
            b = int(lg["blockNumber"])
            tx = lg.get("transactionHash")
            txh = tx.hex() if tx is not None else ""
            name = tmap.get(lg.get("topics", [None])[0], "event")
            try:
                ts = int(W3.eth.get_block(b)["timestamp"])
            except Exception:
                ts = 0
            out.append({"block": str(b), "timestamp": str(ts), "event": name, "tx": txh})
        return fr, to, out

    rows = []
    with ThreadPoolExecutor(max_workers=min(150, max(2, len(ranges)))) as ex:
        futs = {ex.submit(fetch_range, fr, to): (fr, to) for (fr, to) in ranges}
        for i, fut in enumerate(as_completed(futs), 1):
            fr, to = futs[fut]
            try:
                _fr, _to, out = fut.result()
            except Exception as e:
                print(f"    logs {fr}-{to}: failed: {e}")
                continue
            rows.extend(out)
            if i % 10 == 0 or i == len(futs):
                print(f"    logs {fr}-{to}: +{len(out)} ({i}/{len(futs)})")

    rows.sort(key=lambda r: int(r["block"]))
    return rows


def write_csv(path: Path, rows: list, header: list):
    new = not path.exists()
    # If file exists, honor its header to keep columns stable
    use_header = header
    if path.exists():
        try:
            with path.open() as f:
                reader = csv.reader(f)
                first = next(reader, None)
                if first:
                    use_header = first
        except Exception:
            pass
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=use_header)
        if new:
            w.writeheader()
        for r in rows:
            # align row to header
            aligned = {k: r.get(k, "") for k in use_header}
            w.writerow(aligned)


# ---------------- States fetching ----------------

def get_mc(rpc_url: str):
    return Multicall(provider_url=rpc_url, batch=200, max_retries=3, gas_limit=50_000_000)


def to_int(x):
    try:
        return int(x)
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return None


def collect_view_functions(abi: list):
    """Collect zero-input view/pure functions whose outputs are not addresses.

    - Includes functions returning ints, tuples of ints, arrays of ints
    - Skips any function whose any output type includes 'address'
    - Skips strings/bools/bytes
    Returns list of function names.
    """
    fns = []
    for item in abi:
        if item.get("type") != "function":
            continue
        if item.get("stateMutability") not in ("view", "pure"):
            continue
        if item.get("inputs"):
            continue
        outputs = item.get("outputs", [])
        if not outputs:
            continue
        def ok(o):
            t = o.get("type", "")
            if "address" in t:
                return False
            if t.startswith(("uint", "int")):
                return True
            if t.startswith("tuple"):
                comps = o.get("components", [])
                return all(c.get("type", "").startswith(("uint", "int")) and ("address" not in c.get("type", "")) for c in comps)
            if t.endswith("[]"):  # arrays
                base = t[:-2]
                return base.startswith(("uint", "int"))
            return False
        if not all(ok(o) for o in outputs):
            continue
        name = item.get("name")
        if name:
            fns.append(name)
    return fns


def fetch_states(W3: Web3, mc: Multicall, cp_addr: str, lt_addr: str, amm_addr: str, blocks: list[int]):
    # Load ABIs
    cp_abi = fetch_abi(cp_addr)
    lt_abi = fetch_abi(lt_addr)
    amm_abi = fetch_abi(amm_addr)
    cp = W3.eth.contract(address=Web3.to_checksum_address(cp_addr), abi=cp_abi)
    lt = W3.eth.contract(address=Web3.to_checksum_address(lt_addr), abi=lt_abi)
    amm = W3.eth.contract(address=Web3.to_checksum_address(amm_addr), abi=amm_abi)

    # Discover view functions dynamically (zero-input, non-address outputs)
    cp_views = collect_view_functions(cp_abi)
    lt_views = collect_view_functions(lt_abi)
    amm_views = collect_view_functions(amm_abi)

    # Static labels order for all rows
    labels = [*(f"cp.{n}" for n in cp_views), *(f"lt.{n}" for n in lt_views), *(f"amm.{n}" for n in amm_views)]

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def row_for_block(b: int):
        ts = to_int(W3.eth.get_block(b)["timestamp"]) if b is not None else None
        calls = []
        addrs = []
        # cp
        for fn_name in cp_views:
            try:
                calls.append(getattr(cp.functions, fn_name)())
                addrs.append(cp.address)
            except Exception:
                # pad with a dummy call? we skip and later align by order; better to keep positions consistent, so append a harmless call: totalSupply()
                try:
                    calls.append(cp.functions.totalSupply())
                    addrs.append(cp.address)
                except Exception:
                    pass
        # lt
        for fn_name in lt_views:
            try:
                calls.append(getattr(lt.functions, fn_name)())
                addrs.append(lt.address)
            except Exception:
                try:
                    calls.append(lt.functions.totalSupply())
                    addrs.append(lt.address)
                except Exception:
                    pass
        # amm
        for fn_name in amm_views:
            try:
                calls.append(getattr(amm.functions, fn_name)())
                addrs.append(amm.address)
            except Exception:
                try:
                    calls.append(amm.functions.get_debt())
                    addrs.append(amm.address)
                except Exception:
                    pass

        res = mc.aggregate(calls, block_identifier=b, use_try=True, addresses=addrs)
        row = {"block": str(b), "timestamp": str(ts)}
        for lab, val in zip(labels, res):
            try:
                if isinstance(val, (list, tuple)):
                    row[lab] = json.dumps([to_int(x) for x in val])
                else:
                    row[lab] = str(to_int(val))
            except Exception:
                row[lab] = ""
        return row

    rows = []
    with ThreadPoolExecutor(max_workers=min(150, max(2, len(blocks)))) as ex:
        futs = {ex.submit(row_for_block, int(b)): int(b) for b in blocks}
        for i, fut in enumerate(as_completed(futs), 1):
            b = futs[fut]
            try:
                row = fut.result()
            except Exception as e:
                print(f"  states block {b} failed: {e}")
                continue
            rows.append(row)
            if i % 25 == 0 or i == len(futs):
                print(f"  states fetched {i}/{len(futs)}")

    rows.sort(key=lambda r: int(r["block"]))
    header = ["block", "timestamp", *labels]
    return rows, header


# ---------------- Main ----------------

def main():
    if not WEB3_URL:
        raise SystemExit("Set WEB3_PROVIDER_URL or ETH_RPC_URL in env")
    W3 = Web3(Web3.HTTPProvider(WEB3_URL))
    mc = get_mc(WEB3_URL)
    for market_id in POOL_IDS:
        # Discover cp/lt/amm from factory
        cp_addr, lt_addr, amm_addr = discover_market_by_id(W3, market_id)
        # Derive a simple key from LT symbol
        try:
            lt_abi_for_name = fetch_abi(lt_addr)
            lt_name_contract = W3.eth.contract(address=lt_addr, abi=lt_abi_for_name)
            sym = lt_name_contract.functions.symbol().call()
            sym_u = (sym or "").upper()
            if "WBTC" in sym_u:
                key = "wbtc"
            elif "TBTC" in sym_u:
                key = "tbtc"
            elif "CBBTC" in sym_u or "CBBTC" in sym_u:
                key = "cbbtc"
            else:
                key = f"pool{market_id}"
        except Exception:
            key = f"pool{market_id}"

        print(f"Pool {key} (id={market_id})")
        ensure_dirs(key)
        print(f"  cp={cp_addr} lt={lt_addr} amm={amm_addr}")

        cp_abi = fetch_abi(cp_addr)
        lt_abi = fetch_abi(lt_addr)

        # Events (resumable): start from last+1 if file exists
        ev_path = DATA_ROOT / key / "events.csv"
        scan_start = START_BLOCK
        if ev_path.exists():
            try:
                ev_df = pd.read_csv(ev_path)
                if not ev_df.empty and 'block' in ev_df.columns:
                    scan_start = max(START_BLOCK, int(ev_df['block'].max()) + 1)
            except Exception:
                pass
        effective_end = END_BLOCK if END_BLOCK is not None else int(W3.eth.block_number)
        rows = []
        if scan_start <= effective_end:
            cp_events = scan_events(W3, cp_addr, cp_abi, ["AddLiquidity", "RemoveLiquidity", "TokenExchange"], scan_start, effective_end)
            lt_events = scan_events(W3, lt_addr, lt_abi, ["Deposit", "Withdraw"], scan_start, effective_end)
            for r in cp_events:
                rr = dict(r)
                rr["contract"] = "cp"
                rows.append(rr)
            for r in lt_events:
                rr = dict(r)
                rr["contract"] = "lt"
                rows.append(rr)
            rows.sort(key=lambda r: (int(r["block"]), r.get("contract", "")))
            if rows:
                write_csv(ev_path, rows, header=["block", "timestamp", "contract", "event", "tx"])
        print(f"  events written/updated → {ev_path}")

        # Boundary blocks: must include ALL events in file (resume-safe)
        ev_blocks = []
        try:
            all_ev = pd.read_csv(ev_path)
            if not all_ev.empty and 'block' in all_ev.columns:
                ev_blocks = sorted(set(int(b) for b in all_ev['block'].dropna().astype('int64').tolist()))
        except Exception:
            # fallback to just-written rows in this run
            ev_blocks = sorted({int(r["block"]) for r in rows})
        effective_end = END_BLOCK if END_BLOCK is not None else int(W3.eth.block_number)
        boundary = sorted({START_BLOCK, effective_end, *ev_blocks})
        # Also add pre-event blocks (b-1) for state-before
        state_blocks = sorted({b for b in boundary if START_BLOCK <= b <= effective_end} | {max(START_BLOCK, b-1) for b in ev_blocks})

        # States (resumable): skip blocks already present
        st_path = DATA_ROOT / key / "states.csv"
        missing_blocks = state_blocks
        existing_blocks = set()
        if st_path.exists():
            try:
                st_df = pd.read_csv(st_path, usecols=['block'])
                existing_blocks = set(int(b) for b in st_df['block'].dropna().astype('int64').tolist())
            except Exception:
                existing_blocks = set()
        missing_blocks = [b for b in state_blocks if int(b) not in existing_blocks]
        if missing_blocks:
            st_rows, hdr = fetch_states(W3, mc, cp_addr, lt_addr, amm_addr, missing_blocks)
            write_csv(st_path, st_rows, header=hdr)
            print(f"  states written (new): {len(st_rows)} → {st_path}")
        else:
            print(f"  states up-to-date → {st_path}")


if __name__ == "__main__":
    main()
