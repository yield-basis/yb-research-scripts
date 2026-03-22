import pylab

import numpy as np
from brownie import multicall
from datetime import datetime
from brownie import Contract, config
from brownie import web3

config['autofetch_sources'] = True

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
START_BLOCK = 23784145 + 100
N_POINTS = 500
k = 4


def main():
    mc = Contract("0x5BA1e12693Dc8F9c48aAD8770482f4739bEeD696")

    factory = Contract(FACTORY)
    market = factory.markets(k)
    lt = Contract(market[3])
    label = lt.symbol()

    current_block = web3.eth.block_number
    blocks = [int(b) for b in np.linspace(START_BLOCK, current_block, N_POINTS)]

    times = []
    unstaked_pps = []
    redemption_pps = []

    for b in blocks:
        with multicall(address=mc.address, block_identifier=b):
            t = mc.getCurrentBlockTimestamp()
            pps_u = lt.pricePerShare()
            pps_r = lt.preview_withdraw(10**18)

        t = datetime.fromtimestamp(t)
        print(t)
        times.append(t)
        unstaked_pps.append(pps_u / 1e18)
        redemption_pps.append(pps_r / 1e8)

    pylab.plot(times, unstaked_pps, c="black", label="Fundamental value")
    pylab.plot(times, redemption_pps, c="gray", label="Redemption value")

    pylab.title(f"Share prices of pool {label}")

    pylab.grid()
    pylab.legend()
    pylab.xticks(rotation=45, ha='right')
    pylab.xlabel('Time')
    pylab.ylabel('Pool growth')

    pylab.tight_layout()
    pylab.show()
