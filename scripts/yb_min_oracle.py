# Script which tests an oracle to find a lower estimate for LT in YB pool
from brownie import Contract, config
from brownie import web3

config['autofetch_sources'] = True


FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
POOL_ID = 5  # cbBTC
START_BLOCK = 23784065 + 500


def main():
    factory = Contract(FACTORY)
    market = factory.markets(POOL_ID)
    lt = Contract(market[3])
    amm = Contract(market[2])
    cryptopool = Contract(amm.COLLATERAL())
    lp_oracle = Contract(amm.PRICE_ORACLE_CONTRACT())

    # get lower price limit
    p_o = cryptopool.price_oracle()
    p_s = cryptopool.price_scale()
    D = cryptopool.D()
    cryptopool_supply = cryptopool.totalSupply()
    # For stablecoin pool:
    # p_min = virtual_price * min(1, p)
    lp_price_min = D / cryptopool_supply * min(1, p_o / p_s)
    p_lp = lp_oracle.price() / 1e18

    collateral, debt, x0 = amm.get_state()
    # (x0 - debt) * y = I = x0**2 / p_lp * (L / (2L - 1))**2
    # (x0 - debt) / y = lp_price_min
    # We want to find v_o = (y * lp_price_min - debt)
    # -> v_o = x0 - 2 * debt

    # (x0 - debt)**2 = x0**2 * lp_price_min / p_lp  * (L / (2L - 1))**2
    # x0 - debt = x0 * L / (2L - 1) * sqrt(lp_price_min / p_lp)
    # debt = x0 - x0 * L / (2L - 1) * sqrt(lp_price_min / p_lp)
    # v_o = x0 * (2 * L / (2L - 1) * sqrt(lp_price_min / p_lp) - 1)

    print("LP price from pool:", cryptopool.lp_price() / 1e18)
    print("p_lp:", p_lp)
    print("Min LP price:", lp_price_min)

    L = 2
    v_o = x0 / 1e18 * (2 * L / (2*L - 1) * (lp_price_min / p_lp)**0.5 - 1)
    print("Value in AMM by p_s:", amm.value_oracle()[1] / 1e18)
    print("Min value in AMM:", v_o)
