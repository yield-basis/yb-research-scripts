"""Benchmark RPC transport options against the local node.

Issues the SAME workload (an eth_call to Multicall3 at random historical
blocks — archive-heavy, like all_users_pnl.py does) over three transports:

  1. HTTP serial             — one request per HTTP POST, sequential
  2. HTTP batched            — N requests in one HTTP POST (JSON-RPC batch)
  3. WebSocket serial        — one persistent connection, send/recv per call

Reports requests/sec and total time. Run before refactoring all_users_pnl.py
to know whether HTTP batching or WebSocket is worth the wiring.

Usage:
    uv run python scripts/bench_rpc.py [N_REQUESTS] [BATCH_SIZE]
    # default 200 requests, batch_size 50
    # WebSocket URL: $ETH_WS_URL or derived from ETH_RPC_URL with :8546
"""
from __future__ import annotations

import json
import os
import random
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()


HTTP_URL = os.environ["ETH_RPC_URL"]
WS_URL = os.environ.get("ETH_WS_URL")
if WS_URL is None:
    # Try common geth/erigon convention: same host, port 8546, ws scheme.
    WS_URL = (HTTP_URL.replace("http://", "ws://")
                       .replace("https://", "wss://")
                       .replace(":8545", ":8546"))

# A trivial Multicall3 view call: getCurrentBlockTimestamp().
# Selector 0x0f28c97d — returns the block timestamp. Tiny but still requires
# the node to serve eth_call at the historical block.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
CALLDATA = "0x0f28c97d"


def make_payload(req_id: int, block: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "eth_call",
        "params": [
            {"to": MULTICALL3, "data": CALLDATA},
            hex(block),
        ],
    }


def random_blocks(n: int, low: int = 24_500_000, high: int = 25_000_000) -> list[int]:
    random.seed(42)
    return [random.randint(low, high) for _ in range(n)]


def bench_http_serial(blocks: list[int]) -> tuple[float, int]:
    sess = requests.Session()
    t0 = time.time()
    for i, b in enumerate(blocks):
        r = sess.post(HTTP_URL, json=make_payload(i, b), timeout=30)
        r.raise_for_status()
        r.json()
    return time.time() - t0, len(blocks)


def bench_http_batch(blocks: list[int], batch_size: int) -> tuple[float, int]:
    sess = requests.Session()
    t0 = time.time()
    for i in range(0, len(blocks), batch_size):
        batch = blocks[i:i + batch_size]
        payload = [make_payload(i + j, b) for j, b in enumerate(batch)]
        r = sess.post(HTTP_URL, json=payload, timeout=60)
        r.raise_for_status()
        results = r.json()
        if len(results) != len(batch):
            raise RuntimeError(f"batch returned {len(results)} for {len(batch)}")
    return time.time() - t0, len(blocks)


def bench_ws_serial(blocks: list[int]) -> tuple[float, int]:
    try:
        from websockets.sync.client import connect  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(f"websockets package missing: {e}")
    t0 = time.time()
    with connect(WS_URL, open_timeout=15) as ws:
        for i, b in enumerate(blocks):
            ws.send(json.dumps(make_payload(i, b)))
            _ = json.loads(ws.recv(timeout=30))
    return time.time() - t0, len(blocks)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    batch_size = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    blocks = random_blocks(n)
    print(f"HTTP_URL: {HTTP_URL}")
    print(f"WS_URL:   {WS_URL}")
    print(f"N requests: {n}  batch_size: {batch_size}")
    print()

    def report(name: str, dt: float, count: int) -> None:
        print(f"  {name:24s} {dt:>7.2f}s   {count / dt:>7.1f} req/s")

    print("Running HTTP serial...")
    try:
        dt, c = bench_http_serial(blocks)
        report("HTTP serial", dt, c)
    except Exception as e:
        print(f"  HTTP serial failed: {e}")

    print("Running HTTP batched...")
    try:
        dt, c = bench_http_batch(blocks, batch_size)
        report(f"HTTP batched (size={batch_size})", dt, c)
    except Exception as e:
        print(f"  HTTP batched failed: {e}")

    print("Running WebSocket serial...")
    try:
        dt, c = bench_ws_serial(blocks)
        report("WebSocket serial", dt, c)
    except Exception as e:
        print(f"  WebSocket serial failed: {e}")
        print(f"  (set ETH_WS_URL in .env if 8546 is wrong)")


if __name__ == "__main__":
    main()
