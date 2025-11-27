#!/usr/bin/env python3

import json
import csv


pool_names = ['WBTC', 'cbBTC', 'tBTC']

with open('pnl-log.json', 'r') as f:
    pnl_data = json.load(f)

with open('user-pnl.json', 'r') as f:
    user_data = json.load(f)

for idx, name in enumerate(pool_names):
    charged = pnl_data['admin_fees'][idx][-1]
    fair = pnl_data['fair_admin_fees'][idx][-1]
    overcharge = charged - fair
    loss = sum(-v for v in user_data[idx].values() if v < 0)

    print(f'Pool {name} overcharged in admin fee: {overcharge:.4f} BTC')
    print(f'  - Staked users lost: {loss:.4f}')
    print(f'  - Fair admin fee: {fair:.4f}')

    # Assume that we return the overcharge

    with open(f'overcharge-return-{name}.csv', 'w') as f:
        fieldnames = ['Address', 'Return amount']
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for addr, amount in user_data[idx].items():
            if amount < 0:
                refund = -amount / loss * overcharge
                writer.writerow({'Address': addr, 'Return amount': refund})
