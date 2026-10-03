"""Shared helpers for the YB fundamental-PnL study (BTC markets, plus WETH per asset).

Markets (Factory 0x370a…00c0, all on mainnet):

    v1   0 WBTC   1 cbBTC   2 tBTC    twocrypto v2.1.0d pools, LT values at price_oracle
    v2   3 WBTC   4 cbBTC   5 tBTC    SAME cryptopools as v1, LT values at price_scale
    v2   6 WETH                       v2 LT/AMM code, its own v2.1.0d pool
    v3   7 WBTC   8 cbBTC   9 tBTC    new twocrypto v3.0.0 pools (reserved_profit_fraction)
    v3  10 WETH                       v3 code

The "system" totals are over the BTC markets only; WETH is reported per asset.

Valuation ("pools working as designed")
---------------------------------------
The whole system's equity of one market, users + admin together, is what LT
itself books as `cur_value` in `_calculate_values`:

    E = AMM.value_oracle().value / (price_scale * agg)        [asset units, 1e18]

where value_oracle = get_x0(lp_price * agg, collateral, debt) / (2L - 1) and
lp_price = 2 * virtual_price * sqrt(price_scale).

"As designed" replaces the pool's actual `virtual_price` by the part of it that
the pool is *guaranteed* to keep for LPs, i.e. the rebalancing floor:

    v2.1.0d (v1, v2):  vp_floor = max(1, (1 + xcp_profit) / 2)   (half of gross profit)
    v3.0.0  (v3):      vp_floor = lp_xcp_profit                   (reserved_profit_fraction)

Anything above the floor is profit the pool is allowed to burn on rebalancing,
so it is not counted. This is the same correction the older brownie scripts
apply with `ADJUST = True` (collateral * (1 + xcp) / (2 * vp)), generalised to
the v3 pool code.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from functools import cache
from math import isqrt
from pathlib import Path

import eth_abi
from boa.rpc import EthereumRPC
from dotenv import load_dotenv
from eth_utils import keccak, to_checksum_address

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
PICS = HERE / "pics"

load_dotenv(HERE / ".env")

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
MC3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
CHAINLINK_BTCUSD = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"  # 8 decimals

VERSIONS = {0: "v1", 1: "v1", 2: "v1",
            3: "v2", 4: "v2", 5: "v2", 6: "v2",
            7: "v3", 8: "v3", 9: "v3", 10: "v3"}
MARKETS = sorted(VERSIONS)

LOGS_RANGE = 10_000   # node: eth_getLogs block range limit
RPC_BATCH = 100       # node: --rpc.batch.limit
WAD = 10**18


def selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def topic(sig: str) -> str:
    return "0x" + keccak(text=sig).hex()


# --------------------------------------------------------------------- RPC

def rpc_url() -> str:
    url = os.environ.get("ETH_RPC_URL")
    if not url:
        raise SystemExit("ETH_RPC_URL not set (see .env.example)")
    return url


_tl = threading.local()


def rpc() -> EthereumRPC:
    """Per-thread boa RPC client (the client isn't shared-safe)."""
    client = getattr(_tl, "rpc", None)
    if client is None:
        client = _tl.rpc = EthereumRPC(rpc_url())
    return client


def fetch_multi_retry(payloads, attempts: int = 5):
    last = None
    for attempt in range(attempts):
        try:
            return rpc().fetch_multi(payloads)
        except Exception as e:  # transient node hiccups; back off and retry
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"RPC batch failed after {attempts} attempts: {last}")


def head_block() -> int:
    return int(rpc().fetch("eth_blockNumber", []), 16)


def eth_call(to: str, sig: str, types=(), args=(), out=("uint256",), block="latest"):
    data = selector(sig) + (eth_abi.encode(list(types), list(args)) if types else b"")
    res = rpc().fetch("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])
    dec = eth_abi.decode(list(out), bytes.fromhex(res[2:]))
    return dec[0] if len(out) == 1 else dec


# --------------------------------------------------------------- markets

@dataclass(frozen=True, slots=True)
class Market:
    idx: int
    version: str
    symbol: str
    asset: str
    decimals: int
    cryptopool: str
    amm: str
    lt: str
    price_oracle: str
    agg: str
    staker: str
    created_block: int

    @property
    def label(self) -> str:
        return f"{self.version} {self.symbol}"


MARKET_PARAMETERS = topic(
    "MarketParameters(uint256,address,address,address,address,address,address,address,address)")


@cache
def creation_blocks() -> dict[int, int]:
    """Factory MarketParameters event block for each market index."""
    head = head_block()
    payloads = [("eth_getLogs", [{"address": FACTORY, "fromBlock": hex(b),
                                  "toBlock": hex(min(b + LOGS_RANGE - 1, head)),
                                  "topics": [MARKET_PARAMETERS]}])
                for b in range(23_400_000, head + 1, LOGS_RANGE)]
    out: dict[int, int] = {}
    for i in range(0, len(payloads), RPC_BATCH):
        for logs in fetch_multi_retry(payloads[i:i + RPC_BATCH]):
            for log in logs:
                out.setdefault(int(log["topics"][1], 16), int(log["blockNumber"], 16))
    return out


def is_btc(symbol: str) -> bool:
    return "BTC" in symbol


@cache
def yb_markets() -> list[Market]:
    created = creation_blocks()
    markets = []
    for i in MARKETS:
        m = eth_call(FACTORY, "markets(uint256)", ["uint256"], [i],
                     out=["(address,address,address,address,address,address,address)"])
        asset, cp, amm, lt, oracle, _vpool, staker = (to_checksum_address(a) for a in m)
        symbol = eth_call(asset, "symbol()", out=["string"])
        decimals = eth_call(asset, "decimals()")
        agg = to_checksum_address(eth_call(oracle, "AGG()", out=["address"]))
        markets.append(Market(i, VERSIONS[i], symbol, asset, decimals, cp, amm, lt, oracle, agg,
                              staker, created[i]))
    return markets


# ----------------------------------------------------------- LEVAMM math

def get_x0(p_oracle: int, collateral: int, debt: int, lev_ratio: int) -> int:
    """AMM.get_x0(..., safe_limits=False) for an 18-decimal collateral (LP)."""
    coll_value = p_oracle * collateral // WAD
    d = coll_value**2 - 4 * coll_value * lev_ratio // WAD * debt
    return (coll_value + isqrt(max(d, 0))) * WAD // (2 * lev_ratio)


def amm_value(p_oracle: int, collateral: int, debt: int, leverage: int) -> int:
    """AMM.value_oracle_for(collateral, debt).value with an explicit LP price."""
    denominator = 2 * leverage - WAD
    lev_ratio = leverage**2 * WAD // denominator**2
    return get_x0(p_oracle, collateral, debt, lev_ratio) * WAD // denominator


def lp_price(virtual_price: int, price_scale: int) -> int:
    """CryptopoolLPOracle.lp_price() (before the agg multiplier)."""
    return 2 * virtual_price * isqrt(price_scale * WAD) // WAD


def vp_floor(version: str, xcp_profit: int, lp_xcp_profit: int | None) -> int:
    """Virtual price the pool is guaranteed to keep for LPs (see module doc)."""
    if version == "v3":
        return lp_xcp_profit
    return max(WAD, (WAD + xcp_profit) // 2)


# ------------------------------------------------------------- volatility

SECONDS_PER_YEAR = 365 * 86400
VOL_STEP = "1h"  # Chainlink BTC/USD moves in >=0.5% steps or hourly heartbeats


def realised_vol(price) -> float:
    """Annualised realised vol of a time-indexed pandas price Series (1h log returns)."""
    import numpy as np

    px = price.resample(VOL_STEP).last().ffill().dropna()
    r = np.log(px).diff().dropna()
    return float(r.std() * np.sqrt(SECONDS_PER_YEAR / 3600)) if len(r) > 10 else float("nan")


# --------------------------------------------------- LT value split (admin)

MIN_STAKED_FOR_FEES = 10**16
SQRT_MIN_UNSTAKED_FRACTION = 10**14


def _tdiv(a: int, b: int) -> int:
    """Vyper int256 division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def _mul_div_signed(x: int, y: int, d: int) -> int:
    if d == 0:
        return 0
    v = abs(x) * abs(y) // abs(d)
    return -v if ((x < 0) != (y < 0)) != (d < 0) else v


def lt_calculate_values(prev_admin: int, prev_total: int, prev_ideal_staked: int,
                        prev_staked: int, staked: int, supply: int, min_admin_fee: int,
                        cur_value: int) -> tuple[int, int, int]:
    """LT._calculate_values (identical in v1/v2/v3) -> (admin, total, supply_tokens).

    `cur_value` is the market's whole equity in LT's own units:
    value_oracle * 1e18 // LT._price_oracle(). `admin` is the admin's accrued,
    not yet withdrawn, share of it (liquidity.admin as if checkpointed now).
    """
    f_a = 10**18 - (10**18 - min_admin_fee) * isqrt(10**36 - staked * 10**36 // supply) // 10**18
    value_change = cur_value - (prev_total + prev_admin)
    v_st_loss = max(prev_ideal_staked - prev_staked, 0)
    if staked >= MIN_STAKED_FOR_FEES:
        if value_change > 0:
            v_loss = min(value_change, v_st_loss * supply // staked)
            dv_use_36 = v_loss * 10**18 + (value_change - v_loss) * (10**18 - f_a)
        else:
            dv_use_36 = value_change * 10**18
    else:
        dv_use_36 = value_change * (10**18 - f_a)
    admin = prev_admin + (value_change - _tdiv(dv_use_36, 10**18))

    dv_s_36 = _mul_div_signed(dv_use_36, staked, supply)
    if dv_use_36 > 0:
        dv_s_36 = min(dv_s_36, v_st_loss * 10**18)
    new_total_36 = max(prev_total * 10**18 + dv_use_36, 0)
    new_staked_36 = max(prev_staked * 10**18 + dv_s_36, 0)

    den = new_total_36 - new_staked_36
    reduction = (_mul_div_signed(new_total_36, staked, den)
                 - _mul_div_signed(new_staked_36, supply, den))
    max_reduction = abs(_tdiv(_tdiv(value_change * supply, prev_total + value_change + 1)
                              * (10**18 - f_a), SQRT_MIN_UNSTAKED_FRACTION))
    if staked > 0:
        reduction = min(reduction, staked - 1)
    if supply > 0:
        reduction = min(reduction, supply - 1)
    reduction = min(reduction, max_reduction) if reduction >= 0 else max(reduction, -max_reduction)
    if den < 10**4 * 10**18:
        reduction = max(reduction, 0)
    return admin, new_total_36 // 10**18, supply - reduction
