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
N_POINTS = 1000


def main():
    mc = Contract("0x5BA1e12693Dc8F9c48aAD8770482f4739bEeD696")

    factory = Contract(FACTORY)
    n = factory.market_count()
    markets = [factory.markets(i) for i in range(n)]
    lts = [Contract(m[3]) for m in markets]
    amms = [Contract(m[2]) for m in markets]
    cryptopools = [Contract(amm.COLLATERAL()) for amm in amms]
    collateral_decimals = [Contract(lt.ASSET_TOKEN()).decimals() for lt in lts]

    current_block = web3.eth.block_number
    blocks = [int(b) for b in np.linspace(START_BLOCK, current_block, N_POINTS)]

    times = []
    imbalances = defaultdict(list)
    debts = defaultdict(list)

    for b in blocks:
        params = defaultdict(dict)
        with multicall(address=mc.address, block_identifier=b):
            t = mc.getCurrentBlockTimestamp()
            for i in range(n):
                params[i]['b0'] = cryptopools[i].balances(0)
                params[i]['b1'] = cryptopools[i].balances(1)
                params[i]['p_o'] = cryptopools[i].price_oracle()
                params[i]['amm_debt'] = amms[i].get_debt()

        t = datetime.fromtimestamp(t)
        print(t)
        times.append(t)

        for i in range(n):
            pool_value = params[i]['b1'] * 10**(18 - collateral_decimals[i]) * params[i]['p_o'] / 1e18 + params[i]['b0']
            imbalance = params[i]['b0'] / pool_value
            imbalances[i].append(imbalance)
            debts[i].append(params[i]['amm_debt'] / pool_value)

    for i in range(n):
        # pylab.plot(times, np.array(imbalances[i]) * 100, label=lts[i].symbol())
        pylab.plot(times, np.array(debts[i]) * 100, label=lts[i].symbol())

    pylab.title('YB cryptopools imbalance of debt in volatility')
    pylab.ylabel('Debt fraction in AMM [%]')
    pylab.xticks(rotation=45, ha='right')
    pylab.legend()
    pylab.tight_layout()
    pylab.show()
