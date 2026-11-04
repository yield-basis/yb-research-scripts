"""Traversal helpers for Yield Basis markets.

Architecture:
    Factory  →  Market[i] = (asset_token, cryptopool, amm, lt, price_oracle,
                             virtual_pool, staker)
    where:
        amm          → LEVAMM (constant-leverage AMM, AMM.vy)
        lt           → leveraged-token / borrow controller (LT.vy)
        staker       → LiquidityGauge (dao/LiquidityGauge.vy, ERC4626 over LT)
        cryptopool   → underlying Curve twocrypto pool
        price_oracle → LP price oracle (CryptopoolLPOracle.vy)

Factory ENS: factory.yieldbasis.eth
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cache

from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

FACTORY_ENS = "factory.yieldbasis.eth"

# YB token TGE / first airdrop on Ethereum mainnet — first batch distribution
# tx (0xab8629e7…) inside the ~4-hour Multisend window of 2025-10-15.
# Block timestamp: 2025-10-15 09:42:23 UTC (Unix 1760521343).
AIRDROP_1_BLOCK = 23582267

# Start of the per-user PnL window (UTC). PnL counts from the state as of this
# moment, i.e. after the last block at or before it (23582964, 12:02:11).
# v1 markets were already live then, so their positions are picked up mid-life;
# v2 (2025-11-12) and v3 (2026-05-25) start empty inside the window.
WINDOW_START = "2025-10-15 12:02:22"

# Market index -> YB version. v1 and v2 share the same twocrypto pools, but v1 LT
# books at the pool's EMA price_oracle, so v1 pricePerShare drifts far from what
# a withdrawal pays whenever price_scale lags (up to +44% in Feb 2026); v2/v3
# LT book at price_scale.
VERSIONS = {0: "v1", 1: "v1", 2: "v1",
            3: "v2", 4: "v2", 5: "v2", 6: "v2",
            7: "v3", 8: "v3", 9: "v3", 10: "v3"}


def version(idx: int) -> str:
    return VERSIONS.get(idx, "v3+")


def parse_utc(s: str) -> int:
    """'YYYY-MM-DD HH:MM:SS' (UTC unless an offset is given) or unix seconds."""
    if s.isdigit():
        return int(s)
    dt = datetime.fromisoformat(s)
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())

# Addresses to exclude from per-user PnL analysis even though they hold LT
# tokens. These are non-user contracts where the LT (and any YB rewards
# claimed on its behalf) are unrescuable — counting them inflates loss
# totals against addresses that aren't really "users".
EXCLUDED_WALLETS = [
    # Uniswap v4 hook with no token-rescue function in its ABI; verified
    # via classify_users.py + Etherscan ABI inspection.
    "0x000000000004444c5dc75cb358380d2e3de08a90",
]
EXCLUDED_WALLETS = [a.lower() for a in EXCLUDED_WALLETS]

FACTORY_ABI = [
    {
        "name": "market_count",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "markets",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "i", "type": "uint256"}],
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"name": "asset_token", "type": "address"},
                    {"name": "cryptopool", "type": "address"},
                    {"name": "amm", "type": "address"},
                    {"name": "lt", "type": "address"},
                    {"name": "price_oracle", "type": "address"},
                    {"name": "virtual_pool", "type": "address"},
                    {"name": "staker", "type": "address"},
                ],
            }
        ],
    },
    {
        "name": "fee_receiver",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "address"}],
    },
    {
        "name": "MarketParameters",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "idx", "type": "uint256"},
            {"indexed": True, "name": "asset_token", "type": "address"},
            {"indexed": True, "name": "cryptopool", "type": "address"},
            {"indexed": False, "name": "amm", "type": "address"},
            {"indexed": False, "name": "lt", "type": "address"},
            {"indexed": False, "name": "price_oracle", "type": "address"},
            {"indexed": False, "name": "virtual_pool", "type": "address"},
            {"indexed": False, "name": "staker", "type": "address"},
            {"indexed": False, "name": "agg", "type": "address"},
        ],
    },
]


@dataclass(frozen=True, slots=True)
class Market:
    idx: int
    asset_token: str
    cryptopool: str
    amm: str
    lt: str
    price_oracle: str
    virtual_pool: str
    staker: str


@cache
def w3() -> Web3:
    """Web3 client, tries ETH_RPC_URL then falls back to ETH_RPC_URL_FALLBACK."""
    primary = os.environ["ETH_RPC_URL"]
    fallback = os.environ.get("ETH_RPC_URL_FALLBACK")
    client = Web3(Web3.HTTPProvider(primary))
    if client.is_connected():
        return client
    if fallback:
        print(f"⚠ primary RPC unreachable ({primary}); using fallback")
        client = Web3(Web3.HTTPProvider(fallback))
        if client.is_connected():
            return client
    raise RuntimeError(
        f"No reachable RPC: tried {primary}"
        + (f" and {fallback}" if fallback else "")
    )


@cache
def rpc_url() -> str:
    """Return the URL of the currently-active provider (primary or fallback)."""
    return w3().provider.endpoint_uri  # type: ignore[attr-defined]


@cache
def factory_address() -> str:
    addr = w3().ens.address(FACTORY_ENS)
    if addr is None:
        raise RuntimeError(f"Could not resolve ENS {FACTORY_ENS}")
    return addr


@cache
def factory():
    return w3().eth.contract(address=factory_address(), abi=FACTORY_ABI)


def market_count() -> int:
    return factory().functions.market_count().call()


def get_market(i: int) -> Market:
    raw = factory().functions.markets(i).call()
    return Market(i, *raw)


def all_markets() -> list[Market]:
    return [get_market(i) for i in range(market_count())]


@cache
def fee_receiver() -> str:
    """Address that receives YB protocol fees (FeeDistributor)."""
    return factory().functions.fee_receiver().call()


@cache
def block_at(ts: int) -> int:
    """Last block with timestamp <= ts: the state 'as of' ts (binary search)."""
    client = w3()
    low, high = 0, client.eth.block_number
    if client.eth.get_block(high)["timestamp"] <= ts:
        return high
    while high - low > 1:  # invariant: timestamp(low) <= ts < timestamp(high)
        mid = (low + high) // 2
        if client.eth.get_block(mid)["timestamp"] <= ts:
            low = mid
        else:
            high = mid
    return low


@cache
def market_deploy_block(idx: int) -> int:
    """First block where market[idx]'s LT contract has code on chain.

    Binary search on eth_getCode (~25 RPC calls). We avoid eth_getLogs
    because some nodes cap the filter range at 1000 blocks.
    """
    market = get_market(idx)
    client = w3()
    low, high = 0, client.eth.block_number
    while low < high:
        mid = (low + high) // 2
        if client.eth.get_code(market.lt, block_identifier=mid) == b"":
            low = mid + 1
        else:
            high = mid
    return low
