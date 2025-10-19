# This does not pretend to be entirely correct as it misses growth happening if deposits/withdrawals are too close
# This is only made to check the growth between events

# Result for normal operation of AMM: ~Oct 5 - Oct 19
# WBTC - 15.8% APY
# cbBTC - 21.1% APY
# tBTC - 13.0% APY

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
START_BLOCK = 23434125 + 1000
BATCH_SIZE = 500


def main():
    mc = Contract("0x5BA1e12693Dc8F9c48aAD8770482f4739bEeD696")

    factory = Contract(FACTORY)
    n = factory.market_count()
    markets = [factory.markets(i) for i in range(n)]
    lts = [Contract(m[3]) for m in markets]
    amms = [Contract(m[2]) for m in markets]
    cryptopools = [Contract(amm.COLLATERAL()) for amm in amms]
    labels = [lt.symbol() for lt in lts]

    current_block = web3.eth.block_number
    times = [[datetime.fromtimestamp(web3.eth.get_block(START_BLOCK).timestamp)] for i in range(n)]
    growth_oracle_values = [[1.0] for i in range(n)]
    growth_scale_values = [[1.0] for i in range(n)]
    tblocks = [[START_BLOCK] for i in range(n)]
    growth_oracle = [1.0] * n
    growth_scale = [1.0] * n

    growth_scale_adj = [1.0] * n
    growth_scale_values_adj = [[1.0] for i in range(n)]

    for idx in range(n):
        lt = lts[idx]
        amm = amms[idx]
        pool = cryptopools[idx]

        for block in range(START_BLOCK, current_block - (BATCH_SIZE - 1), BATCH_SIZE):
            transfers = []
            deposits = lt.events.Deposit.get_logs(fromBlock=block, toBlock=block+BATCH_SIZE-1)
            withdrawals = lt.events.Withdraw.get_logs(fromBlock=block, toBlock=block+BATCH_SIZE-1)
            blocks = set([ev['blockNumber'] for ev in deposits] + [ev['blockNumber'] for ev in withdrawals])

            batch_start = block
            batch_end = min(current_block, block + BATCH_SIZE)
            blocks.add(block)
            blocks.add(batch_end)
            blocks = sorted(blocks)
            to_value = 0
            to_oracle = 0
            to_scale = 0

            for from_block, to_block in zip(blocks[:-1], blocks[1:]):
                to_block -= 1

                if to_block > from_block:
                    with multicall(address=mc.address, block_identifier=from_block):
                        from_value = amm.value_oracle()
                        from_oracle = pool.price_oracle()
                        from_scale = pool.price_scale()
                        from_xcp = pool.xcp_profit()
                        from_vp = pool.get_virtual_price()
                        from_debt = amm.get_debt()
                        from_collateral = amm.collateral_amount()

                    with multicall(address=mc.address, block_identifier=to_block):
                        to_value = amm.value_oracle()
                        to_oracle = pool.price_oracle()
                        to_scale = pool.price_scale()
                        time = mc.getCurrentBlockTimestamp()
                        to_xcp = pool.xcp_profit()
                        to_vp = pool.get_virtual_price()
                        to_debt = amm.get_debt()
                        to_collateral = amm.collateral_amount()

                    from_collateral = from_collateral * (10**18 + from_xcp) // (2 * from_vp)
                    to_collateral = to_collateral * (10**18 + to_xcp) // (2 * to_vp)

                    from_value_adj = amm.value_oracle_for(from_collateral, from_debt, block_identifier=from_block)[1]
                    to_value_adj = amm.value_oracle_for(to_collateral, to_debt, block_identifier=to_block)[1]

                    tblocks[idx].append(to_block)
                    times[idx].append(datetime.fromtimestamp(time))

                    from_value_oracle = from_value[1] / from_oracle
                    from_value_scale = from_value[1] / from_scale
                    to_value_oracle = to_value[1] / to_oracle
                    to_value_scale = to_value[1] / to_scale
                    growth_oracle_mul = (to_value_oracle / from_value_oracle)
                    scale_oracle_mul = (to_value_scale / from_value_scale)
                    growth_oracle[idx] *= growth_oracle_mul
                    growth_scale[idx] *= scale_oracle_mul
                    growth_oracle_values[idx].append(growth_oracle[idx])
                    growth_scale_values[idx].append(growth_scale[idx])

                    from_value_adj /= from_scale
                    to_value_adj /= to_scale
                    growth_mul_adj = to_value_adj / from_value_adj
                    growth_scale_adj[idx] *= growth_mul_adj
                    growth_scale_values_adj[idx].append(growth_scale_adj[idx])

                    print(times[idx][-1], labels[idx])

    colors = ['orange', 'blue', 'gray']
    for idx in range(n):
        plt.plot(times[idx], growth_scale_values_adj[idx], label=labels[idx], c=colors[idx]) 

    ax = plt.gca()
    ax.yaxis.set_major_formatter(ScalarFormatter(useOffset=False))

    plt.title("Fundamental value growth in YB pools")
    plt.xticks(rotation=45, ha='right')
    plt.legend()
    plt.tight_layout()
    plt.show()
