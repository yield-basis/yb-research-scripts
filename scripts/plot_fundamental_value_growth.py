# This does not pretend to be entirely correct as it misses growth happening if deposits/withdrawals are too close
# This is only made to check the growth between events

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
BATCH_SIZE = 500
POOL_ID = 1  # cbbtc


def main():
    mc = Contract("0x5BA1e12693Dc8F9c48aAD8770482f4739bEeD696")

    factory = Contract(FACTORY)
    n = factory.market_count()
    markets = [factory.markets(i) for i in range(n)]
    lts = [Contract(m[3]) for m in markets]
    amms = [Contract(m[2]) for m in markets]
    cryptopools = [Contract(amm.COLLATERAL()) for amm in amms]
    lt = lts[POOL_ID]
    amm = amms[POOL_ID]
    pool = cryptopools[POOL_ID]

    current_block = web3.eth.block_number
    times = [datetime.fromtimestamp(web3.eth.get_block(START_BLOCK).timestamp)]
    growth_oracle_values = [1.0]
    growth_scale_values = [1.0]
    tblocks = [START_BLOCK]
    growth_oracle = 1.0
    growth_scale = 1.0

    growth_scale_adj = 1.0
    growth_scale_values_adj = growth_scale_values[:]

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

                tblocks.append(to_block)
                times.append(datetime.fromtimestamp(time))

                from_value_oracle = from_value[1] / from_oracle
                from_value_scale = from_value[1] / from_scale
                to_value_oracle = to_value[1] / to_oracle
                to_value_scale = to_value[1] / to_scale
                growth_oracle_mul = (to_value_oracle / from_value_oracle)
                scale_oracle_mul = (to_value_scale / from_value_scale)
                growth_oracle *= growth_oracle_mul
                growth_scale *= scale_oracle_mul
                growth_oracle_values.append(growth_oracle)
                growth_scale_values.append(growth_scale)

                from_value_adj /= from_scale
                to_value_adj /= to_scale
                growth_mul_adj = to_value_adj / from_value_adj
                growth_scale_adj *= growth_mul_adj
                growth_scale_values_adj.append(growth_scale_adj)

                print(times[-1], from_block, to_block, growth_oracle, growth_scale)  # , growth_oracle_mul, scale_oracle_mul)

    pylab.plot(times, growth_scale_values, label="scale") 
    pylab.plot(times, growth_scale_values_adj, label="scale adjusted") 
    pylab.plot(times, growth_oracle_values, label="oracle") 
    pylab.xticks(rotation=45, ha='right')
    pylab.legend()
    pylab.tight_layout()
    pylab.show()
