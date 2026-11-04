"""Compute YB token distribution.

Constants (edit at top of file):
  YB_TO_DISTRIBUTE        — total YB to allocate
  COMPENSATION_FRACTION   — share of the pool earmarked for loss-compensation

Allocation:
  • COMPENSATION bucket goes to users whose net PnL (USD, all markets, after
    entry/exit costs) is negative, proportional to |loss|.
  • PROPORTIONAL bucket goes to ALL users (including those with losses),
    proportional to their share of all users' value integrated over time
    (`days` from value_integral.py).
  • Each user's yb_total = yb_compensation + yb_proportional.

Inputs (both per address, HybridVaults merged into their owners, the same
non-user addresses excluded; they should share an end block):
  pnl_per_address_usd.csv   net_pnl_after_entry_exit_usd (net_pnl_usd if
                            all_users_pnl.py ran without --entry-exit)
  value_integral.csv        days since AIRDROP_1_BLOCK (value_integral.py)

Output: yb_distribution.csv with columns
    [user, days, pnl_usd, yb_compensation, yb_proportional, yb_total]

Usage:
    uv run python scripts/yb_distribution.py [--pnl FILE] [--integral FILE] [--out FILE]
"""
from __future__ import annotations

import sys
import time as _time

import polars as pl


YB_TO_DISTRIBUTE = 5_000_000  # Just a number for example
COMPENSATION_FRACTION = 0.5   # Just a number for example


def _log(msg: str) -> None:
    print(f"[{_time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> None:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        sys.exit(0)
    pnl_csv = "pnl_per_address_usd.csv"
    integral_csv = "value_integral.csv"
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
         f"{yb_prop_total:>12,.0f} YB  (to all users ∝ days)")

    pnl_raw = pl.read_csv(pnl_csv)
    pnl_col = ("net_pnl_after_entry_exit_usd" if "net_pnl_after_entry_exit_usd"
               in pnl_raw.columns else "net_pnl_usd")
    pnl = pnl_raw.select("user", pl.col(pnl_col).alias("pnl_usd"))
    integral_raw = pl.read_csv(integral_csv)
    integral = integral_raw.select("user", "days")
    pnl_block = pnl_raw["as_of_block"][0]
    int_block = integral_raw["to_block"][0]
    _log(f"{pnl_csv}: {len(pnl)} users, {pnl_col}, as of block {pnl_block}")
    _log(f"{integral_csv}: {len(integral)} users, to block {int_block}")
    if pnl_block != int_block:
        _log(f"⚠ different end blocks ({pnl_block} vs {int_block}): "
             f"rerun value_integral.py after all_users_pnl.py")

    df = (pl.DataFrame({"user": sorted(set(pnl["user"]) | set(integral["user"]))})
            .join(integral, on="user", how="left")
            .join(pnl, on="user", how="left")
            .with_columns(pl.col("days").fill_null(0.0), pl.col("pnl_usd").fill_null(0.0)))
    _log(f"merged: {len(df)} unique users")

    # --- Compensation bucket ---
    df = df.with_columns(
        loss=pl.when(pl.col("pnl_usd") < 0).then(-pl.col("pnl_usd")).otherwise(0.0),
    )
    total_loss = float(df["loss"].sum())
    n_losers = int((df["loss"] > 0).sum())
    _log(f"losers: {n_losers}  Σ|loss| = ${total_loss:,.2f}")
    df = df.with_columns(
        yb_compensation=pl.when(pl.lit(total_loss) > 0)
                        .then(yb_comp_total * pl.col("loss") / total_loss)
                        .otherwise(pl.lit(0.0)),
    )

    # --- Proportional bucket ---
    total_days = float(df["days"].sum())
    n_active = int((df["days"] > 0).sum())
    _log(f"active in window: {n_active}  Σ days = {total_days:.6f}")
    df = df.with_columns(
        yb_proportional=pl.when(pl.lit(total_days) > 0)
                        .then(yb_prop_total * pl.col("days") / total_days)
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
