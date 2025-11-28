from datetime import datetime
from brownie import multicall
from brownie import Contract, config
from brownie import web3

config['autofetch_sources'] = True

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
MULTICALL = "0x5BA1e12693Dc8F9c48aAD8770482f4739bEeD696"


def main():
    factory = Contract(FACTORY)
    n = factory.market_count()
    markets = [factory.markets(i) for i in range(n)]
    lts = [Contract(m[3]) for m in markets]
    amms = [Contract(m[2]) for m in markets]
    cryptopools = [Contract(amm.COLLATERAL()) for amm in amms]
    gauges = [Contract(m[-1]) for m in markets]
    labels = [lt.symbol() for lt in lts]

    def liquidity_coefficient(idx, block):
        liquidity = lts[idx].liquidity(block_identifier=block)
        return liquidity[1] / (liquidity[0] + liquidity[1])

    def unstaked_pps(idx, block, adjusted=False):
        pool = cryptopools[idx]
        amm = amms[idx]
        lt = lts[idx]

        if not adjusted:
            adjustment = 1.0
        else:
            with multicall(address=MULTICALL, block_identifier=block):
                xcp = pool.xcp_profit()
                vp = pool.get_virtual_price()
            adjustment = (10**18 + xcp) / (2 * vp)
        with multicall(address=MULTICALL, block_identifier=block):
            collateral = amm.collateral_amount()
            debt = amm.get_debt()
            liquidity = lt.liquidity()
            ps = pool.price_scale()
            lt_supply = lt.totalSupply()

        collateral *= adjustment
        value = amm.value_oracle_for(collateral, debt, block_identifier=block)[1] / ps
        value *= min(liquidity[1] / (liquidity[0] + liquidity[1]), 1.0)

        return value / (lt_supply / 1e18)

    def staked_pps(idx, block, adjusted=False):
        staked_ratio = gauges[idx].previewRedeem(10**18, block_identifier=block) / 1e18
        return unstaked_pps(idx, block, adjusted) * staked_ratio

    import IPython
    IPython.embed()
