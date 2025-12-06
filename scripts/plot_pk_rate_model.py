import matplotlib.pyplot as plt

from math import exp
from brownie import multicall
from datetime import datetime
from brownie import Contract, config
from brownie import web3

from brownie import ZERO_ADDRESS

config['autofetch_sources'] = True


MULTICALL = "0x5BA1e12693Dc8F9c48aAD8770482f4739bEeD696"
# START_BLOCK = 23800000
START_BLOCK = 21_500_000
MONETARY_POLICY = "0x8c5A7F011f733fBb0A6c969c058716d5CE9bc933"
DEBT_RATIO_EMA = 3 * 7 * 86400
N_PER_EMA = 70


def main():
    mc = Contract(MULTICALL)
    mp = Contract(MONETARY_POLICY)

    price_oracle = Contract(mp.PRICE_ORACLE())

    end_block = web3.eth.block_number
    blocks = list(range(START_BLOCK, end_block, max(DEBT_RATIO_EMA // 13 // N_PER_EMA, 1)))

    times = []
    rates_before = []
    rates_after = []
    debt_ratio_ma = mp.target_debt_fraction() / 1e18

    for block in blocks:
        with multicall(address=MULTICALL, block_identifier=block):
            pk_addrs = [mp.peg_keepers(pk_id) for pk_id in range(5)]
            controllers = [mp.controllers(i) for i in range(15)]

        pks = [Contract(addr) for addr in pk_addrs if addr != ZERO_ADDRESS]
        controllers = [Contract(addr) for addr in controllers if addr != ZERO_ADDRESS]

        with multicall(address=MULTICALL, block_identifier=block):
            time = mc.getCurrentBlockTimestamp()
            sigma = mp.sigma()
            target_debt_fraction = mp.target_debt_fraction()
            rate0 = mp.rate0()
            pk_debts = [pk.debt() for pk in pks]
            controller_debts = [controller.total_debt() for controller in controllers]
            p_o = price_oracle.price()

        total_debt = sum(controller_debts)
        pk_debt = sum(pk_debts)

        debt_ratio = pk_debt / total_debt
        if len(times) > 0:
            ema_mul = exp(-(time - times[-1]) / DEBT_RATIO_EMA)
        else:
            ema_mul = 0
        debt_ratio_ma = debt_ratio_ma * ema_mul + debt_ratio * (1 - ema_mul)
        prev_debt_ratio = debt_ratio
        power = (1e18 - p_o) / sigma
        power_before = power - debt_ratio / (target_debt_fraction / 1e18)
        power_after = power - debt_ratio_ma / (target_debt_fraction / 1e18)

        rate_before = rate0 / 1e18 * (365 * 86400) * exp(power_before)
        rate_after = rate0 / 1e18 * (365 * 86400) * exp(power_after)

        times.append(time)
        rates_before.append(100 * rate_before)
        rates_after.append(100 * rate_after)

        print(datetime.fromtimestamp(time), block)

    times = [datetime.fromtimestamp(t) for t in times]

    plt.plot(times, rates_before, c="gray", label="No EMA")
    plt.plot(times, rates_after, c="black", label="With EMA on (pk_debt/total_debt)")
    plt.legend()
    plt.title(f"Borrow rate (EMA time = {DEBT_RATIO_EMA // 86400} days)")
    plt.tick_params("x", rotation=45)
    plt.ylabel("[%]")

    plt.tight_layout()
    plt.show()
