import matplotlib.pyplot as plt

from brownie import multicall
from datetime import datetime
from brownie import Contract, config
from brownie import web3

config['autofetch_sources'] = True


MULTICALL = "0x5BA1e12693Dc8F9c48aAD8770482f4739bEeD696"
START_BLOCK = 23575554
N_POINTS = 500

POOLS = {
    'usdc': "0x4DEcE678ceceb27446b35C672dC7d61F30bAD69E",
    'usdt': "0x390f3595bCa2Df7d23783dFd126427CCeb997BF4",
    'frxusd': "0x13e12BB0E6A2f1A3d6901a59a9d585e89A6243e1",
    'pyusd': "0x625E92624Bc2D88619ACCc1788365A69767f6200"
}
GAUGES = {
    'usdc': "0x95f00391cB5EebCd190EB58728B4CE23DbFa6ac1",
    'usdt': "0x4e6bB6B7447B7B2Aa268C16AB87F4Bb48BF57939",
    'frxusd': "0x22804B0F6bE741a9Fa1BbaEcDD6c8D4116E96944",
    'pyusd': "0xf69Fb60B79E463384b40dbFDFB633AB5a863C9A2"
}


def main():
    mc = Contract(MULTICALL)

    pools = {name: Contract(addr) for name, addr in POOLS.items()}
    gauges = {name: Contract(addr) for name, addr in GAUGES.items()}

    end_block = web3.eth.block_number
    blocks = list(range(START_BLOCK, end_block, (end_block - START_BLOCK) // N_POINTS))

    fig, _axes = plt.subplots(1, len(pools), sharey=False, sharex=False)
    axes = {}
    for name, ax in zip(pools.keys(), _axes):
        axes[name] = ax

    all_supplies = {k: [] for k in pools.keys()}
    times = []
    all_gauge_balances = {k: [] for k in pools.keys()}

    for block in blocks:
        supplies = {}
        gauge_balances = {}
        vprices = {}

        with multicall(address=MULTICALL, block_identifier=block):
            time = mc.getCurrentBlockTimestamp()
            for name in pools.keys():
                pool = pools[name]
                gauge = gauges[name]
                supplies[name] = pool.totalSupply()
                gauge_balances[name] = pool.balanceOf(gauge.address)
                vprices[name] = pool.get_virtual_price()

        time = datetime.fromtimestamp(time)
        print(time, block)

        for name in pools.keys():
            supply = supplies[name] / 1e18 * vprices[name] / 1e18 / 1e6
            gauge_balance = gauge_balances[name] / 1e18 * vprices[name] / 1e18 / 1e6
            all_supplies[name].append(supply)
            all_gauge_balances[name].append(gauge_balance)

        times.append(time)

    for name in pools.keys():
        ax = axes[name]
        ax.plot(times, all_supplies[name], c="gray")
        ax.plot(times, all_gauge_balances[name], c="blue")
        ax.set_title(name)
        ax.tick_params("x", rotation=45)
        ax.set_ylabel("Supply (millions USD)")

    fig.tight_layout()
    plt.show()
