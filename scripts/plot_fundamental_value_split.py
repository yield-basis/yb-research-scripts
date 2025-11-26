# This does not pretend to be entirely correct as it misses growth happening if deposits/withdrawals are too close
# This is only made to check the growth between events

# Result for normal operation of AMM: ~Oct 5 - Oct 19
# WBTC - 15.8% APY
# cbBTC - 21.1% APY
# tBTC - 13.0% APY

import matplotlib.pyplot as plt
from matplotlib.ticker import ScalarFormatter

from brownie import multicall
from datetime import datetime
from brownie import Contract, config
from brownie import web3

import json

config['autofetch_sources'] = True

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
START_BLOCK = 23434125 + 1000  # <- XXX should we start closer to deployment?
BATCH_SIZE = 500
ADJUST = True


def merge_feeds(times, values):
    time_to_value = []
    merged_times = set()
    for idx in range(len(times)):
        time_to_value.append(
            {t: v for t, v in zip(times[idx], values[idx])}
        )
        merged_times.update(times[idx])
    merged_times = sorted(list(merged_times))
    running_values = [0] * len(times)
    output_values = []
    for t in merged_times:
        for idx in range(len(times)):
            if t in time_to_value[idx]:
                running_values[idx] = time_to_value[idx][t]
        output_values.append(sum(running_values))
    return merged_times, output_values


def main():
    mc = Contract("0x5BA1e12693Dc8F9c48aAD8770482f4739bEeD696")

    factory = Contract(FACTORY)
    n = 3
    markets = [factory.markets(i) for i in range(n)]
    lts = [Contract(m[3]) for m in markets]
    amms = [Contract(m[2]) for m in markets]
    cryptopools = [Contract(amm.COLLATERAL()) for amm in amms]
    stakers = [Contract(lt.staker()) for lt in lts]
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
    earned_profits = [[0.0] for i in range(n)]
    admin_fees = [[0.0] for i in range(n)]
    fair_admin_fees = [[0.0] for i in range(n)]
    staked_fractions = [[0.0] for i in range(n)]
    staked_pnl = [[0.0] for i in range(n)]
    unstaked_pnl = [[0.0] for i in range(n)]

    for idx in range(n):
        lt = lts[idx]
        amm = amms[idx]
        pool = cryptopools[idx]
        staker = stakers[idx]

        staked_pps = None
        unstaked_pps = 1.0
        staked_deposits = None
        admin_fees_withdrawn = 0

        for block in range(START_BLOCK, current_block - (BATCH_SIZE - 1), BATCH_SIZE):
            deposits = lt.events.Deposit.get_logs(fromBlock=block, toBlock=block+BATCH_SIZE-1)
            withdrawals = lt.events.Withdraw.get_logs(fromBlock=block, toBlock=block+BATCH_SIZE-1)
            stakes = staker.events.Deposit.get_logs(fromBlock=block, toBlock=block+BATCH_SIZE-1)
            unstakes = staker.events.Withdraw.get_logs(fromBlock=block, toBlock=block+BATCH_SIZE-1)
            waf = lt.events.WithdrawAdminFees.get_logs(fromBlock=block, toBlock=block+BATCH_SIZE-1)
            blocks = set(
                    [ev['blockNumber'] for ev in deposits]
                    + [ev['blockNumber'] for ev in withdrawals]
                    + [ev['blockNumber'] for ev in stakes]
                    + [ev['blockNumber'] for ev in unstakes]
            )

            admin_fees_events = {w['blockNumber']: w['args']['amount'] for w in waf}

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
                        # from_liquidity = lt.liquidity()  # (admin, total, ideal_staked, staked)
                        # from_staked = lt.balanceOf(staker)
                        # from_supply = lt.totalSupply()

                    with multicall(address=mc.address, block_identifier=to_block):
                        to_value = amm.value_oracle()
                        to_oracle = pool.price_oracle()
                        to_scale = pool.price_scale()
                        time = mc.getCurrentBlockTimestamp()
                        to_xcp = pool.xcp_profit()
                        to_vp = pool.get_virtual_price()
                        to_debt = amm.get_debt()
                        to_collateral = amm.collateral_amount()
                        liquidity = to_liquidity = lt.liquidity()  # (admin, total, ideal_staked, staked)
                        staked = to_staked = lt.balanceOf(staker)
                        supply = to_supply = lt.totalSupply()
                        min_admin_fee = lt.min_admin_fee()
                        staker_supply = staker.totalSupply()

                    from_collateral = int(from_collateral * ((10**18 + from_xcp) / (2 * from_vp) if ADJUST else 1))
                    to_collateral = int(to_collateral * ((10**18 + to_xcp) / (2 * to_vp) if ADJUST else 1))

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
                    d_profit = to_value_adj - from_value_adj
                    earned_profits[idx].append(earned_profits[idx][-1] + d_profit)

                    d_staked_value = 0
                    d_unstaked_value = 0
                    useful_value = to_value_adj * to_liquidity[1] / (to_liquidity[0] + to_liquidity[1])
                    new_staked_pps = None
                    if staker_supply > 0:
                        new_staked_pps = useful_value * to_staked / to_supply / (staker_supply / 1e18)
                    if staked_pps is not None:
                        d_staked_value = staked_deposits * (new_staked_pps / staked_pps - 1)
                    staked_deposits = useful_value * to_staked / to_supply
                    staked_pps = new_staked_pps
                    staked_pnl[idx].append(staked_pnl[idx][-1] + d_staked_value)

                    new_unstaked_pps = useful_value / (to_supply / 1e18)
                    if len(staked_fractions[idx]) > 1:
                        d_unstaked_value = (new_unstaked_pps - unstaked_pps) * (to_supply - to_staked) / 1e18
                    else:
                        d_unstaked_value = 0
                    unstaked_pps = new_unstaked_pps
                    unstaked_pnl[idx].append(unstaked_pnl[idx][-1] + d_unstaked_value)

                    staked_fractions[idx].append(staked / supply)

                    f_a = 1.0 - (1.0 - min_admin_fee / 1e18) * (1.0 - staked / supply)**0.5
                    admin_fees_addition = admin_fees_withdrawn + sum(v for b, v in admin_fees_events.items() if b <= to_block)
                    admin_fees_addition *= unstaked_pps
                    admin_fees[idx].append(
                            (liquidity[0] + admin_fees_addition) / 1e18
                    )
                    fair_admin_fees[idx].append(fair_admin_fees[idx][-1] + d_profit * f_a)

                    print(times[idx][-1], labels[idx])

            admin_fees_withdrawn += sum(admin_fees_events.values()) * unstaked_pps

    # Save all data
    with open('pnl-log.json', 'w') as f:
        json.dump({
            'n': n,
            'blocks': tblocks,
            'times': times,
            'earned_profits': earned_profits,
            'admin_fees': admin_fees,
            'fair_admin_fees': fair_admin_fees,
            'staked_pnl': staked_pnl,
            'unstaked_pnl': unstaked_pnl
        }, f)

    fig, ((ax_rel, ax_charged_admin, ax_staked_pnl), (ax_abs, ax_fair_admin, ax_unstaked_pnl)) = plt.subplots(2, 3, sharey=False, sharex=True)

    colors = ['orange', 'blue', 'gray']
    for idx in range(n):
        ax_rel.plot(times[idx], growth_scale_values_adj[idx], label=labels[idx], c=colors[idx])

    merged_times, earned_profits_sum = merge_feeds(times, earned_profits)
    ax_abs.plot(merged_times, earned_profits_sum, c="black")
    merged_times, admin_fees_sum = merge_feeds(times, admin_fees)
    ax_charged_admin.plot(merged_times, admin_fees_sum, c="black")
    merged_times, fair_admin_fees_sum = merge_feeds(times, fair_admin_fees)
    ax_fair_admin.plot(merged_times, fair_admin_fees_sum, c="black")
    merged_times, staked_pnl_sum = merge_feeds(times, staked_pnl)
    ax_staked_pnl.plot(merged_times, staked_pnl_sum, c="black")
    merged_times, unstaked_pnl_sum = merge_feeds(times, unstaked_pnl)
    ax_unstaked_pnl.plot(merged_times, unstaked_pnl_sum, c="black")

    ax_rel.set_title("Relative growth")
    ax_abs.set_title("Net system profit [BTC]")
    ax_charged_admin.set_title("Admin fees charged [BTC]")
    ax_fair_admin.set_title("Correct admin fees [BTC]")
    ax_staked_pnl.set_title("Staked PnL [BTC]")
    ax_unstaked_pnl.set_title("Unstaked PnL [BTC]")

    ax_rel.yaxis.set_major_formatter(ScalarFormatter(useOffset=False))
    ax_rel.legend()
    ax_rel.tick_params("x", rotation=45)
    ax_abs.tick_params("x", rotation=45)
    ax_charged_admin.tick_params("x", rotation=45)
    ax_fair_admin.tick_params("x", rotation=45)
    ax_unstaked_pnl.tick_params("x", rotation=45)

    fig.tight_layout()
    plt.show()
