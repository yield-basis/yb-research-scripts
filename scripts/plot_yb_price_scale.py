import pylab

import numpy as np
from brownie import multicall
from datetime import datetime
from brownie import Contract, config
from brownie import web3

config['autofetch_sources'] = True

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CL_FEED = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"
START_BLOCK = 23784145 + 100
N_POINTS = 500
k = 4


def main():
    mc = Contract("0x5BA1e12693Dc8F9c48aAD8770482f4739bEeD696")

    factory = Contract(FACTORY)
    market = factory.markets(k)
    pool = Contract(market[1])
    lt = Contract(market[3])
    label = lt.symbol()
    feed = Contract(CL_FEED)

    current_block = web3.eth.block_number
    blocks = [int(b) for b in np.linspace(START_BLOCK, current_block, N_POINTS)]

    times = []
    price_scales = []
    prices = []

    for b in blocks:
        with multicall(address=mc.address, block_identifier=b):
            t = mc.getCurrentBlockTimestamp()
            price_scale = pool.price_scale()
            price_feed = feed.latestAnswer()

        t = datetime.fromtimestamp(t)
        print(t)
        times.append(t)
        prices.append(price_feed / 1e8)
        price_scales.append(price_scale / 1e18)

    pylab.plot(times, prices, c="black", label="BTC price")
    pylab.plot(times, price_scales, c="gray", label="price_scale")
    pylab.title(f"price_scale of pool {label}")

    pylab.grid()
    pylab.legend()
    pylab.xticks(rotation=45, ha='right')
    pylab.xlabel('Time')
    pylab.ylabel('BTC price [USD]')

    pylab.tight_layout()
    pylab.show()
