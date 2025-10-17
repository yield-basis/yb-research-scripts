import pylab
import numpy as np
from brownie import multicall
from collections import defaultdict
from datetime import datetime
from brownie import Contract, config
from brownie import web3

config['autofetch_sources'] = True

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
START_BLOCK = 23434125
N_POINTS = 500


def main():
    mc = Contract("0x5BA1e12693Dc8F9c48aAD8770482f4739bEeD696")

    factory = Contract(FACTORY)
    n = factory.market_count()
    markets = [factory.markets(i) for i in range(n)]
    lts = [Contract(m[3]) for m in markets]
    amms = [Contract(m[2]) for m in markets]
    cryptopools = [Contract(amm.COLLATERAL()) for amm in amms]

    current_block = web3.eth.block_number
    blocks = [int(b) for b in np.linspace(START_BLOCK, current_block, N_POINTS)]

    times = []
    pps = defaultdict(list)

    for b in blocks:
        params = defaultdict(dict)
        with multicall(address=mc.address, block_identifier=b):
            t = mc.getCurrentBlockTimestamp()
            for i in range(n):
                params[i]['xcp'] = cryptopools[i].xcp_profit()
                params[i]['vp'] = cryptopools[i].get_virtual_price()
                params[i]['collateral'] = amms[i].collateral_amount()
                params[i]['debt'] = amms[i].get_debt()

        t = datetime.fromtimestamp(t)
        print(t)
        times.append(t)

        for i in range(n):
            params[i]['collateral'] = params[i]['collateral'] * (10**18 + params[i]['xcp']) // (2 * params[i]['vp'])

        with multicall(address=mc.address, block_identifier=b):
            for i in range(n):
                params[i]['value'] = amms[i].value_oracle_for(params[i]['collateral'], params[i]['debt'])
                params[i]['supply'] = lts[i].totalSupply()
                params[i]['liquidity'] = lts[i].liquidity()
                params[i]['p_o'] = cryptopools[i].price_scale()

        for i in range(n):
            value = params[i]['value'][1]
            value = value / (params[i]['p_o'] / 1e18)
            admin, total, ideal_staked, staked = params[i]['liquidity']
            supply = params[i]['supply']
            pps[i].append(
                    (value / supply) * (total / (total + admin))
            )

    for i in range(n):
        pylab.plot(times, pps[i], label=lts[i].symbol())

    pylab.xticks(rotation=45, ha='right')
    pylab.legend()
    pylab.tight_layout()
    pylab.show()
