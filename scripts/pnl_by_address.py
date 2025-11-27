import json
import numpy as np
from datetime import datetime
from brownie import Contract, config
from brownie import web3
from brownie import ZERO_ADDRESS


config['autofetch_sources'] = True

PNL_LOG_FILE = "pnl-log.json"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
START_BLOCK = 23433457
BATCH_SIZE = 500
MARKET_IDS = [0, 1, 2]


def main():
    factory = Contract(FACTORY)
    markets = [factory.markets(i) for i in MARKET_IDS]
    lts = [Contract(m[3]) for m in markets]
    stakers = [Contract(lt.staker()) for lt in lts]
    labels = [lt.symbol() for lt in lts]

    max_block = web3.eth.block_number  # XXX read from json
    start_time = datetime.fromtimestamp(web3.eth.get_block(START_BLOCK).timestamp)
    times = [[start_time] for i in MARKET_IDS]
    
    # market_id -> address -> np.array([balances])
    lt_balances = [{} for i in MARKET_IDS]
    st_balances = [{} for i in MARKET_IDS]

    for idx in MARKET_IDS:
        lt = lts[idx]
        staker = stakers[idx]

        for block in range(START_BLOCK, max_block, BATCH_SIZE):
            to_block = min(block + BATCH_SIZE - 1, max_block)
            lt_transfers = lt.events.Transfer.get_logs(fromBlock=block, toBlock=to_block)
            st_transfers = staker.events.Transfer.get_logs(fromBlock=block, toBlock=to_block)

            for ev in lt_transfers:
                start_idx = ev.blockNumber - START_BLOCK
                if ev.args.sender not in lt_balances[idx]:
                    lt_balances[idx][ev.args.sender] = np.zeros(max_block + 1 - START_BLOCK)
                if ev.args.receiver not in lt_balances[idx]:
                    lt_balances[idx][ev.args.receiver] = np.zeros(max_block + 1 - START_BLOCK)
                lt_balances[idx][ev.args.sender][start_idx:] -= ev.args.value / 1e18
                lt_balances[idx][ev.args.receiver][start_idx:] += ev.args.value / 1e18

            for ev in st_transfers:
                start_idx = ev.blockNumber - START_BLOCK
                if ev.args.sender not in st_balances[idx]:
                    st_balances[idx][ev.args.sender] = np.zeros(max_block + 1 - START_BLOCK)
                if ev.args.receiver not in st_balances[idx]:
                    st_balances[idx][ev.args.receiver] = np.zeros(max_block + 1 - START_BLOCK)
                st_balances[idx][ev.args.sender][start_idx:] -= ev.args.value / 1e18
                st_balances[idx][ev.args.receiver][start_idx:] += ev.args.value / 1e18

            print(f'Pool {labels[idx]}: {(block - START_BLOCK) * 100 / (max_block - START_BLOCK):.1f}%, {len(lt_transfers) + len(st_transfers)} transfers')

    print('=====================')
    for idx in MARKET_IDS:
        st = stakers[idx]
        lt = lts[idx]
        kw = {'block_identifier': max_block}
        expected_supply_lt = (lt.totalSupply(**kw) - lt.balanceOf(st.address, **kw)) / 1e18
        expected_supply_st = st.totalSupply(**kw) / 1e18
        measured_supply_lt = sum(v[-1] for a, v in lt_balances[idx].items() if a not in [ZERO_ADDRESS, st.address])
        measured_supply_st = sum(v[-1] for a, v in st_balances[idx].items() if a not in [ZERO_ADDRESS, st.address])
        print(f'Pool {labels[idx]}:')
        print(f'    * {len(lt_balances[idx]) + len(st_balances[idx])} addresses')
        print(f'    * Unstaked supply: measured = {measured_supply_lt:.4f}, expected = {expected_supply_lt:.4f}')
        print(f'    * Staked supply: measured = {measured_supply_st:.4f}, expected = {expected_supply_st:.4f}')
