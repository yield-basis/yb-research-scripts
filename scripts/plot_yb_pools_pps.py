import matplotlib.pyplot as plt
from matplotlib.ticker import ScalarFormatter

import numpy as np
from brownie import multicall
from collections import defaultdict
from datetime import datetime
from brownie import Contract, config
from brownie import web3

config['autofetch_sources'] = True

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
START_BLOCK = 23784145 + 100
N_POINTS = 500
n = 3


def main():
    mc = Contract("0x5BA1e12693Dc8F9c48aAD8770482f4739bEeD696")

    factory = Contract(FACTORY)
    markets = [factory.markets(i) for i in [3, 4, 5]]
    lts = [Contract(m[3]) for m in markets]
    stakers = [Contract(lt.staker()) for lt in lts]
    labels = [lt.symbol() for lt in lts]

    current_block = web3.eth.block_number
    blocks = [int(b) for b in np.linspace(START_BLOCK, current_block, N_POINTS)]

    times = []
    unstaked_pps = defaultdict(list)
    staked_pps = defaultdict(list)

    for b in blocks:
        pps_u = {}
        pps_s = {}
        with multicall(address=mc.address, block_identifier=b):
            t = mc.getCurrentBlockTimestamp()
            for i in range(n):
                pps_u[i] = lts[i].pricePerShare()
                pps_s[i] = stakers[i].previewRedeem(10**18)

        t = datetime.fromtimestamp(t)
        print(t)
        times.append(t)

        for i in range(n):
            unstaked_pps[i].append(pps_u[i] / 1e18)
            staked_pps[i].append(pps_s[i] / 1e18 * pps_u[i] / 1e18)

    fig, (ax_unstaked, ax_staked) = plt.subplots(1, 2, sharey=True)

    colors = ['orange', 'blue', 'gray']
    for idx in range(n):
        ax_unstaked.plot(times, unstaked_pps[idx], c=colors[idx], label=labels[idx])
        ax_staked.plot(times, staked_pps[idx], c=colors[idx], label=labels[idx])

    ax_unstaked.set_title("Unstaked growth")
    ax_staked.set_title("Staked value change")

    ax_unstaked.yaxis.set_major_formatter(ScalarFormatter(useOffset=False))
    ax_staked.yaxis.set_major_formatter(ScalarFormatter(useOffset=False))

    ax_unstaked.legend()
    ax_staked.legend()

    ax_unstaked.tick_params("x", rotation=45)
    ax_staked.tick_params("x", rotation=45)

    fig.tight_layout()
    plt.show()
