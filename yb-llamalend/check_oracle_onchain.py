"""Check yb_prices.py against the deployed yb-WETH contracts at a few blocks.

At each block, reads the pool, the LevAMM, the crvUSD aggregator and the LT, recomputes:
  weth.oracles.yieldbasis.eth.price()        YBLendingOracle.price_in_asset(LT)
  weth.oracles-ll.yieldbasis.eth.price()     YBLendingOracleLL.price(), from its stored EMA state
  LT.pricePerShare()
  LT.preview_emergency_withdraw(1 LT)        the redemption constituents (WETH, signed crvUSD)
and prints the relative differences (float64 vs the contracts' uint256 math: ~1e-12 is a match).

usage: ETH_RPC_URL=http://... uv run python yb-llamalend/check_oracle_onchain.py [block ...]
"""

import os
import sys

import numpy as np
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yb_prices as yp  # noqa: E402

POOL = "0x656341Ef90b622c6634e0573772FfB7f3669b9f3"
AMM = "0x5f8D24F33Cc5a1D5D1bf012261E6A2214C92233c"
LT = "0x2B9c9f3BdcEb5d8E36a4704F08a78Fca53343cEa"
ENS = {"oracle": "weth.oracles.yieldbasis.eth", "oracle_ll": "weth.oracles-ll.yieldbasis.eth"}


def fn(name, inputs=(), outputs=("uint256",)):
    return {"name": name, "type": "function", "stateMutability": "view",
            "inputs": [{"name": f"a{i}", "type": t} for i, t in enumerate(inputs)],
            "outputs": [{"name": f"o{i}", "type": t} for i, t in enumerate(outputs)]}


POOL_ABI = [fn(n) for n in ("price_oracle", "price_scale", "virtual_price", "D", "totalSupply", "A")] + \
    [fn("balances", ["uint256"])]
AMM_ABI = [fn("collateral_amount"), fn("get_debt"), fn("is_killed", outputs=["bool"]),
           fn("PRICE_ORACLE_CONTRACT", outputs=["address"])]
AGG_ABI = [fn("price"), fn("AGG", outputs=["address"])]
LT_ABI = [fn("liquidity", outputs=["int256", "uint256", "uint256", "uint256"]), fn("totalSupply"),
          fn("staker", outputs=["address"]), fn("balanceOf", ["address"]), fn("min_admin_fee"), fn("pricePerShare"),
          fn("preview_emergency_withdraw", ["uint256"], ["uint256", "int256"]), fn("agg", outputs=["address"])]
ORACLE_ABI = [fn("price"), fn("fundamental_ema"), fn("fundamental_last"), fn("ema_ts"), fn("ema_time")]


def main(blocks):
    w3 = Web3(Web3.HTTPProvider(os.environ["ETH_RPC_URL"], request_kwargs={"timeout": 60}))
    pool, amm, lt = (w3.eth.contract(address=a, abi=abi) for a, abi in ((POOL, POOL_ABI), (AMM, AMM_ABI), (LT, LT_ABI)))
    agg = w3.eth.contract(address=lt.functions.agg().call(), abi=AGG_ABI)
    oracle = w3.eth.contract(address=w3.ens.address(ENS["oracle"]), abi=ORACLE_ABI)
    oracle_ll = w3.eth.contract(address=w3.ens.address(ENS["oracle_ll"]), abi=ORACLE_ABI)
    head = w3.eth.block_number
    blocks = blocks or [head - k * 30_000 for k in range(6)]
    print(f"oracle {oracle.address}, LL {oracle_ll.address}")
    print(f"{'block':>9} {'oracle WETH':>12} {'d oracle':>9} {'LL WETH':>12} {'d LL':>9} {'pps':>12} {'d pps':>9} "
          f"{'redeem WETH':>12} {'d coin1':>9} {'d coin0':>9} {'po/ps-1':>8}")
    worst = 0.0
    for b in blocks:
        c = lambda f, *a: f(*a).call(block_identifier=b)  # noqa: E731
        ts = w3.eth.get_block(b)["timestamp"]
        po, ps, vp, D, S, A = (c(getattr(pool.functions, n)) / (1 if n == "A" else 1e18)
                               for n in ("price_oracle", "price_scale", "virtual_price", "D", "totalSupply", "A"))
        b0, b1 = c(pool.functions.balances, 0) / 1e18, c(pool.functions.balances, 1) / 1e18
        coll, debt, killed = c(amm.functions.collateral_amount) / 1e18, c(amm.functions.get_debt) / 1e18, \
            c(amm.functions.is_killed)
        aggp = c(agg.functions.price) / 1e18
        admin, total, ideal, staked_v = (x / 1e18 for x in c(lt.functions.liquidity))
        staker = c(lt.functions.staker)
        lv0 = yp.LiquidityState(admin=admin, total=total, ideal_staked=ideal, staked=staked_v,
                                supply_tokens=c(lt.functions.totalSupply) / 1e18,
                                staked_tokens=c(lt.functions.balanceOf, staker) / 1e18)
        maf = c(lt.functions.min_admin_fee) / 1e18

        t = yp.oracle_terms(np.array([po]), np.array([ps]), np.array([vp]), np.array([D]), np.array([S]), yp.a_raw(A),
                            np.array([coll]), np.array([debt]), np.array([aggp]), killed)
        lv = yp.calculate_values(lv0, float(t.value_ps[0] / t.p_o_ps[0]), maf)
        k = yp.per_share(lv)
        price = float(t.equity_po[0] * k / t.asset_price[0])
        fundamental = float(t.value_ps[0] * k / t.p_o_ps[0])
        ema, last, ema_ts, ema_time = (c(getattr(oracle_ll.functions, n)) for n in
                                       ("fundamental_ema", "fundamental_last", "ema_ts", "ema_time"))
        f_ema = fundamental
        if ema:
            alpha = np.exp(-(ts - ema_ts) / ema_time)
            f_ema = (last * (1 - alpha) + ema * alpha) / 1e18
        price_ll = price * f_ema / fundamental
        coin1, coin0, redeem = yp.redemption(coll, debt, b0, b1, S, k, po)
        pew1, pew0 = (x / 1e18 for x in c(lt.functions.preview_emergency_withdraw, 10**18))

        rel = lambda ours, theirs: ours / theirs - 1  # noqa: E731
        d = [rel(price, c(oracle.functions.price) / 1e18), rel(price_ll, c(oracle_ll.functions.price) / 1e18),
             rel(lv.total / lv.supply_tokens, c(lt.functions.pricePerShare) / 1e18), rel(coin1, pew1),
             (coin0 - pew0) / coin1]  # coin0 crosses zero: its error relative to the position
        worst = max(worst, *map(abs, d))
        print(f"{b:>9} {price:>12.8f} {d[0]:>9.1e} {price_ll:>12.8f} {d[1]:>9.1e} {lv.total / lv.supply_tokens:>12.8f} "
              f"{d[2]:>9.1e} {redeem:>12.8f} {d[3]:>9.1e} {d[4]:>9.1e} {po / ps - 1:>+8.4f}")
    print(f"worst relative difference {worst:.1e}")


if __name__ == "__main__":
    main([int(b) for b in sys.argv[1:]])
