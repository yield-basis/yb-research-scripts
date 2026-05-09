"""Compute YB token distribution.

Constants (edit at top of file):
  YB_TO_DISTRIBUTE        — total YB to allocate
  COMPENSATION_FRACTION   — share of the pool earmarked for loss-compensation

Allocation:
  • COMPENSATION bucket goes to users with net_pnl_pps < 0 (in BTC-eq),
    proportional to |loss|.
  • PROPORTIONAL bucket goes to ALL users (including those with losses),
    proportional to their btc_blocks integral.
  • Each user's yb_total = yb_compensation + yb_proportional.

Inputs:
  pnl_all_users.csv         per (user, market) net_pnl_pps  (from all_users_pnl.py)
  btc_time_integral.csv     per-user btc_blocks since AIRDROP_1_BLOCK
                            (from btc_time_integral.py)

Both inputs already exclude gauge / LT / fee_receiver / EXCLUDED_WALLETS, so
no further filtering is needed.

Output: yb_distribution.csv with columns
    [user, btc_blocks, pnl_btc, yb_compensation, yb_proportional, yb_total]
"""
from __future__ import annotations

import os
import sys
import time as _time

import polars as pl
from dotenv import load_dotenv
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from yb import all_markets, w3  # noqa: E402

load_dotenv()


YB_TO_DISTRIBUTE = 5_000_000  # Just a number for example
COMPENSATION_FRACTION = 0.5   # Just a number for example

POOL_ABI = [{"name": "price_oracle", "type": "function", "stateMutability": "view",
             "inputs": [], "outputs": [{"type": "uint256"}]}]


def _log(msg: str) -> None:
    print(f"[{_time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    args = sys.argv[1:]
    pnl_csv = "pnl_all_users.csv"
    integral_csv = "btc_time_integral.csv"
    output_csv = "yb_distribution.csv"
    while args:
        if args[0] == "--pnl":
            pnl_csv = args[1]; args = args[2:]
        elif args[0] == "--integral":
            integral_csv = args[1]; args = args[2:]
        elif args[0] == "--out":
            output_csv = args[1]; args = args[2:]
        else:
            break

    yb_comp_total = YB_TO_DISTRIBUTE * COMPENSATION_FRACTION
    yb_prop_total = YB_TO_DISTRIBUTE * (1 - COMPENSATION_FRACTION)
    _log(f"YB pool: {YB_TO_DISTRIBUTE:>12,} YB")
    _log(f"  compensation bucket  ({COMPENSATION_FRACTION:>5.0%}):  "
         f"{yb_comp_total:>12,.0f} YB  (to losers ∝ |loss|)")
    _log(f"  proportional bucket  ({1 - COMPENSATION_FRACTION:>5.0%}):  "
         f"{yb_prop_total:>12,.0f} YB  (to all users ∝ btc_blocks)")

    # --- Per-market BTC factors (current) ---
    client = w3()
    by_idx = {m.idx: m for m in all_markets()}
    factors_crvusd = {}
    for idx in (3, 4, 5, 6):
        cp = client.eth.contract(address=by_idx[idx].cryptopool, abi=POOL_ABI)
        factors_crvusd[idx] = cp.functions.price_oracle().call() / 1e18
    btc_in_crvusd = factors_crvusd[3]
    asset_per_btc = {idx: p / btc_in_crvusd for idx, p in factors_crvusd.items()}
    _log("asset_per_btc (now): " +
         "  ".join(f"M{i}={asset_per_btc[i]:.4f}" for i in (3, 4, 5, 6)))

    # --- Load PnL CSV, aggregate per user in BTC equivalent ---
    pnl = pl.read_csv(pnl_csv).filter(pl.col("market").is_in([3, 4, 5, 6]))
    pnl = pnl.with_columns(
        pnl_btc=pl.col("net_pnl_pps") * pl.col("market").replace_strict(asset_per_btc),
    )
    per_user_pnl = pnl.group_by("user").agg(pl.col("pnl_btc").sum())
    _log(f"{pnl_csv}: {len(pnl)} rows → {len(per_user_pnl)} unique users")

    # --- Load btc_time_integral (already per-user) ---
    integral = pl.read_csv(integral_csv).select(["user", "btc_blocks"])
    _log(f"{integral_csv}: {len(integral)} users")

    # --- Outer-join the two by user ---
    all_users = sorted(set(per_user_pnl["user"].to_list())
                       | set(integral["user"].to_list()))
    df = pl.DataFrame({"user": all_users})
    df = df.join(integral, on="user", how="left")
    df = df.join(per_user_pnl, on="user", how="left")
    df = df.with_columns(
        pl.col("btc_blocks").fill_null(0.0),
        pl.col("pnl_btc").fill_null(0.0),
    )
    _log(f"merged: {len(df)} unique users")

    # --- Compensation bucket ---
    df = df.with_columns(
        loss=pl.when(pl.col("pnl_btc") < 0).then(-pl.col("pnl_btc")).otherwise(0.0),
    )
    total_loss = float(df["loss"].sum())
    n_losers = int((df["loss"] > 0).sum())
    _log(f"losers: {n_losers}  Σ|loss| = {total_loss:.6f} BTC-eq")
    df = df.with_columns(
        yb_compensation=pl.when(pl.lit(total_loss) > 0)
                        .then(yb_comp_total * pl.col("loss") / total_loss)
                        .otherwise(pl.lit(0.0)),
    )

    # --- Proportional bucket ---
    total_btc_blocks = float(df["btc_blocks"].sum())
    n_active = int((df["btc_blocks"] > 0).sum())
    _log(f"active in window: {n_active}  Σ btc_blocks = {total_btc_blocks:.4f}")
    df = df.with_columns(
        yb_proportional=pl.when(pl.lit(total_btc_blocks) > 0)
                        .then(yb_prop_total * pl.col("btc_blocks") / total_btc_blocks)
                        .otherwise(pl.lit(0.0)),
    )

    df = df.with_columns(
        yb_total=pl.col("yb_compensation") + pl.col("yb_proportional"),
    ).drop("loss")

    df = df.sort("yb_total", descending=True)
    df.write_csv(output_csv)

    print()
    _log(f"Σ yb_compensation: {df['yb_compensation'].sum():>13,.4f}  YB  "
         f"(target {yb_comp_total:,.0f})")
    _log(f"Σ yb_proportional: {df['yb_proportional'].sum():>13,.4f}  YB  "
         f"(target {yb_prop_total:,.0f})")
    _log(f"Σ yb_total:        {df['yb_total'].sum():>13,.4f}  YB  "
         f"(target {YB_TO_DISTRIBUTE:,})")
    _log(f"saved {output_csv}  ({len(df)} rows)")

    pl.Config.set_fmt_str_lengths(50)
    pl.Config.set_tbl_rows(20)
    print("\nTop 20 recipients (by yb_total):")
    print(df.head(20))


if __name__ == "__main__":
    main()
