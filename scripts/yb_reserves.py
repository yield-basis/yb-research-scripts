import numpy as np
from brownie import Contract, config
from brownie import web3

config['autofetch_sources'] = True


FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
pool_ids = [3, 4, 5]


def main():
    factory = Contract(FACTORY)
    markets = [factory.markets(i) for i in pool_ids]
    pools = [Contract(m[1]) for m in markets]

    b = web3.eth.block_number
    # b = 23_734_100
    t_plot = web3.eth.get_block(b).timestamp
    print("Block", b)

    for pool in pools:
        xcp = (pool.xcp_profit(block_identifier=b) / 1e18 + 1) / 2
        vp = pool.get_virtual_price(block_identifier=b) / 1e18
        donation_shares = pool.donation_shares(block_identifier=b)
        last_donation_release_ts = pool.last_donation_release_ts(block_identifier=b)
        donation_duration = pool.donation_duration(block_identifier=b)
        unlocked_shares = float(np.clip(
            donation_shares * ((t_plot - last_donation_release_ts) / donation_duration),
            0,
            donation_shares,
        ))
        supply = pool.totalSupply(block_identifier=b)
        reserve_shares = (unlocked_shares + (vp / xcp - 1) * supply)
        reserve_f = reserve_shares / supply
        reserve_usd = reserve_shares * pool.lp_price(block_identifier=b) / (1e18)**2
        decimals = Contract(pool.coins(1)).decimals()
        b0 = pool.balances(0, block_identifier=b) / 1e18
        b1 = pool.balances(1, block_identifier=b) / (10**decimals)
        b1 *= pool.price_oracle(block_identifier=b) / 1e18
        imbalance = abs(0.5 - b0 / (b0 + b1))
        price_shift = abs(1 - pool.price_oracle(block_identifier=b) / pool.price_scale(block_identifier=b))

        print("Pool", pool.name())
        print(f"    Rebalance reserve: {reserve_usd:.2f} USD")
        print(f"    Rebalance reserve:fraction {reserve_f*100:.4f}%")
        print(f"    Imbalance: {imbalance*100:.4f}%")
        print(f"    Price shift: {price_shift*100:.4f}%")
