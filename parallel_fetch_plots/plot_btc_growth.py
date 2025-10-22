
import csv
import json
import math
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import numpy as np

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data"

# Choose pools to plot (folder names under v4/data)
POOL_KEYS = ["wbtc", "tbtc", "cbbtc"]

START_DATE = '2025-10-05'  # e.g., '2025-10-05' (UTC)

data_dict = {}

def extract_data(pool_key):
    print(f"Processing {pool_key}")
    ev_path = DATA_ROOT / pool_key / "events.csv"
    st_path = DATA_ROOT / pool_key / "states.csv"
    if not (ev_path.exists() and st_path.exists()):
        print(f"Missing data for {pool_key}, skipping")
        
    ev = pd.read_csv(ev_path)
    deposit_blocks = set(ev.loc[(ev['contract'].str.lower() == 'lt') & (ev['event'].str.lower().isin(['deposit'])), 'block'].astype('int64'))
    withdraw_blocks = set(ev.loc[(ev['contract'].str.lower() == 'lt') & (ev['event'].str.lower().isin(['withdraw'])), 'block'].astype('int64'))
    print(f'Found {len(deposit_blocks)}/{len(withdraw_blocks)} LT deposit/withdraw events')
    df = pd.read_csv(st_path)
    print(f'Found {len(df)} state snapshots')
    # order by block
    blocks = pd.to_numeric(df['block'], errors='coerce').astype('Int64')
    order = blocks.argsort()
    df = df.iloc[order].reset_index(drop=True)
    blocks = pd.to_numeric(df['block'], errors='coerce').fillna(0).astype('int64')
    times = pd.to_datetime(pd.to_numeric(df['timestamp'], errors='coerce'), unit='s', utc=True)

    # arrays
    # p_o from amm.value_oracle[0]
    vo_vals = df['amm.value_oracle'].to_numpy()
    amm_price_oracle = np.zeros(len(df), dtype=np.float64) # price_oracle (LP shares price based on price_scale
    amm_value_oracle = np.zeros(len(df), dtype=np.float64) # value_oracle x0/(2L-1)

    for i, s in enumerate(vo_vals):
        arr = json.loads(s)
        amm_price_oracle[i] = float(int(arr[0])) / 1e18
        amm_value_oracle[i] = float(int(arr[1])) / 1e18

    cp_virtual_price = pd.to_numeric(df.get('cp.get_virtual_price', df.get('cp.virtual_price', 0)), errors='coerce').fillna(0).to_numpy(dtype=np.float64) / 1e18
    cp_xcp_profit = pd.to_numeric(df.get('cp.xcp_profit', 0), errors='coerce').fillna(0).to_numpy(dtype=np.float64) / 1e18
    amm_debt = pd.to_numeric(df.get('amm.get_debt', 0), errors='coerce').fillna(0).to_numpy(dtype=np.float64) / 1e18
    amm_collateral = pd.to_numeric(df.get('amm.collateral_amount', 0), errors='coerce').fillna(0).to_numpy(dtype=np.float64) / 1e18
    cp_price_scale = pd.to_numeric(df.get('cp.price_scale', 0), errors='coerce').fillna(0).to_numpy(dtype=np.float64) / 1e18
    cp_price_oracle= pd.to_numeric(df.get('cp.price_oracle', 0), errors='coerce').fillna(0).to_numpy(dtype=np.float64) / 1e18
    cp_lp_price = pd.to_numeric(df.get('cp.lp_price', 0), errors='coerce').fillna(0).to_numpy(dtype=np.float64) / 1e18
    lt_total_stables = pd.to_numeric(df['lt.stablecoin_allocated'], errors='coerce').to_numpy(dtype=np.float64) / 10**18

    df_dict = {
        'times': times, 
        'blocks': blocks,
        'amm_price_oracle': amm_price_oracle, 
        'amm_value_oracle': amm_value_oracle,
        'amm_debt': amm_debt,
        'amm_collateral': amm_collateral,
        'cp_virtual_price': cp_virtual_price,
        'cp_xcp_profit': cp_xcp_profit,
        'cp_price_scale': cp_price_scale,
        'cp_price_oracle': cp_price_oracle,
        'cp_lp_price': cp_lp_price,
        'lt_total_stables': lt_total_stables,
        'deposit_blocks': deposit_blocks,
        'withdraw_blocks': withdraw_blocks}


    adj_coefficient = (1.0 + cp_xcp_profit) / (2.0 * cp_virtual_price) #lower bound of lp price drop due to rebalance
    # collateral value according to amm internal logic (ses price_scale of twocrypto pool)
    coll_value_amm = amm_price_oracle * amm_collateral 
    coll_value_amm_adj = amm_price_oracle * amm_collateral * adj_coefficient

    # collateral value according to twocrypto pool internal logic (uses lp_price getter that depends on price_oracle, i.e. closer to spot value)
    coll_value_cp = cp_lp_price * amm_collateral
    coll_value_cp_adj = cp_lp_price * amm_collateral * adj_coefficient
    # adjusted collateral and x0/value
    # recalculate x0/(2L-1) using adjusted lp_price (vp -> xcp_profit)
    lev_ratio = 4.0/9.0
    amm_x0_adj = (coll_value_amm_adj + np.sqrt(coll_value_amm_adj*coll_value_amm_adj - 4.0 * coll_value_amm_adj * lev_ratio * amm_debt)) / (2.0 * lev_ratio)
    amm_value_oracle_adj = amm_x0_adj / 3.0

    # amm value oracle normalized to BTC price (previous plots)
    amm_value_oracle_adj_btc_ps = amm_value_oracle_adj / cp_price_scale
    amm_value_oracle_adj_btc_po = amm_value_oracle_adj / cp_price_oracle

    # pure BTC balances (collateral - debt) / price - how much BTC AMM currently owns
    pure_btc_po = (coll_value_cp_adj - amm_debt) / cp_price_oracle
    pure_btc_ps = (coll_value_amm_adj - amm_debt) / cp_price_scale

    df_dict['amm_value_oracle_adj_btc_ps'] = amm_value_oracle_adj_btc_ps
    df_dict['amm_value_oracle_adj_btc_po'] = amm_value_oracle_adj_btc_po
    df_dict['pure_btc_po'] = pure_btc_po
    df_dict['pure_btc_ps'] = pure_btc_ps

    return df_dict

def compute_growth(target_metric,idx_suppress):
    # Growth via shift; override ratios to 1.0 for idx_suppress
    num = target_metric[1:]
    den = target_metric[:-1]
    with np.errstate(divide='ignore', invalid='ignore'):
        ratio = num / den

    ratio[idx_suppress] = 1.0
    growth = np.concatenate(([1.0], np.cumprod(ratio)))

    return growth


if __name__ == "__main__":
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 10))

    for pool_key in POOL_KEYS:
        df_dict = extract_data(pool_key)
        if START_DATE:
            start_ts = pd.Timestamp(START_DATE).tz_localize('UTC')
            start_idx = np.where(df_dict['times'] >= start_ts)[0][0]

        # must start from 1 because growth is computed on shifted series
        idx_deposit = np.isin(df_dict['blocks'][1:], list(df_dict['deposit_blocks']))
        idx_withdraw = np.isin(df_dict['blocks'][1:], list(df_dict['withdraw_blocks']))
        idx_total_stable_change = np.isin(df_dict['blocks'][1:], df_dict['blocks'][1:][np.diff(df_dict['lt_total_stables']) != 0])
        # we ignore metric changes at deposits/withdrawals and only keep changes due to trading 
        idx_suppress = idx_deposit | idx_withdraw | idx_total_stable_change
        growth_po = compute_growth(df_dict['pure_btc_po'][start_idx:], idx_suppress[start_idx:])
        growth_ps = compute_growth(df_dict['pure_btc_ps'][start_idx:], idx_suppress[start_idx:])
        ax1.plot(df_dict['times'][start_idx:], growth_po, label=pool_key)
        # ax1.plot(df_dict['times'][start_idx:], growth_ps, label=pool_key)
        ax1.set_title("Pure AMM BTC balance growth (LT event blocks excluded)")
        ax1.legend()
        ax2.plot(df_dict['times'][start_idx:], df_dict['cp_price_oracle'][start_idx:], label=pool_key)
        ax2.set_title("BTC price (oracle) per pool")
        ax2.legend()

    fig.autofmt_xdate()
    plt.tight_layout()
    plt.show()
