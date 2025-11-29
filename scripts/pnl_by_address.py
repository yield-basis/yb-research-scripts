import json
import numpy as np
from datetime import datetime
from brownie import Contract, config
from brownie import web3
from brownie import ZERO_ADDRESS


config['autofetch_sources'] = True

PNL_LOG_FILE = "pnl-log.json"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
BATCH_SIZE = 500
MARKET_IDS = [0, 1, 2]


def main():
    with open(PNL_LOG_FILE, 'r') as f:
        data = json.load(f)

    factory = Contract(FACTORY)
    markets = [factory.markets(i) for i in MARKET_IDS]
    lts = [Contract(m[3]) for m in markets]
    stakers = [Contract(lt.staker()) for lt in lts]
    labels = [lt.symbol() for lt in lts]

    start_block = min(d[0] for d in data['blocks'])
    max_block = max(d[-1] for d in data['blocks'])
    start_time = datetime.fromtimestamp(web3.eth.get_block(start_block).timestamp)
    times = [[start_time] for i in MARKET_IDS]
    
    # market_id -> address -> np.array([balances])
    lt_balances = [{} for i in MARKET_IDS]
    st_balances = [{} for i in MARKET_IDS]

    for idx in MARKET_IDS:
        lt = lts[idx]
        staker = stakers[idx]

        for block in range(start_block, max_block, BATCH_SIZE):
            to_block = min(block + BATCH_SIZE - 1, max_block)
            lt_transfers = lt.events.Transfer.get_logs(fromBlock=block, toBlock=to_block)
            st_transfers = staker.events.Transfer.get_logs(fromBlock=block, toBlock=to_block)

            for ev in lt_transfers:
                start_idx = ev.blockNumber - start_block
                if ev.args.sender not in lt_balances[idx]:
                    lt_balances[idx][ev.args.sender] = np.zeros(max_block + 1 - start_block)
                if ev.args.receiver not in lt_balances[idx]:
                    lt_balances[idx][ev.args.receiver] = np.zeros(max_block + 1 - start_block)
                lt_balances[idx][ev.args.sender][start_idx:] -= ev.args.value / 1e18
                lt_balances[idx][ev.args.receiver][start_idx:] += ev.args.value / 1e18

            for ev in st_transfers:
                start_idx = ev.blockNumber - start_block
                if ev.args.sender not in st_balances[idx]:
                    st_balances[idx][ev.args.sender] = np.zeros(max_block + 1 - start_block)
                if ev.args.receiver not in st_balances[idx]:
                    st_balances[idx][ev.args.receiver] = np.zeros(max_block + 1 - start_block)
                st_balances[idx][ev.args.sender][start_idx:] -= ev.args.value / 1e18
                st_balances[idx][ev.args.receiver][start_idx:] += ev.args.value / 1e18

            print(f'Pool {labels[idx]}: {(block - start_block) * 100 / (max_block - start_block):.1f}%, {len(lt_transfers) + len(st_transfers)} transfers')

    print()
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
    print()

    user_pnl = [{} for i in MARKET_IDS]

    for idx in MARKET_IDS:
        addrs = (set(lt_balances[idx].keys()) | set(st_balances[idx].keys())).difference([ZERO_ADDRESS, stakers[idx].address])
        addrs = sorted(addrs)

        for addr in addrs:
            b0 = data['blocks'][idx][0]
            prev_staked_pps = 1.0
            prev_unstaked_pps = 1.0
            user_pnl[idx][addr] = 0.0
            print(f'Processing {labels[idx]}:{addr}...', end='   ')

            for b, staked_pps, unstaked_pps, unstaked_pnl, fair_unstaked_pnl in zip(
                    data['blocks'][idx], data['staked_pps'][idx], data['unstaked_pps'][idx], data['unstaked_pnl'][idx], data['fair_unstaked_pnl'][idx]):
                if staked_pps is None or staked_pps == 0:
                    staked_pps = 1.0
                if unstaked_pps is None or unstaked_pps == 0:
                    unstaked_pps = 1.0

                unstaked_pps_modified = unstaked_pps
                if unstaked_pps > 1:
                    unstaked_pps_modified = 1 + (unstaked_pps - 1) * (1 - fair_unstaked_pnl / unstaked_pnl)
 
                if addr in lt_balances[idx]:
                    user_pnl[idx][addr] += (unstaked_pps_modified - prev_unstaked_pps) * lt_balances[idx][addr][b - b0]
                if addr in st_balances[idx]:
                    user_pnl[idx][addr] += (staked_pps - prev_staked_pps) * st_balances[idx][addr][b - b0]

                prev_staked_pps = staked_pps
                prev_unstaked_pps = unstaked_pps_modified

            print(f'PNL = {user_pnl[idx][addr]}')

    with open('user-pnl.json', 'w') as f:
        json.dump(user_pnl, f)
