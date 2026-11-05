"""Which wallets drop out between value_integral.csv and value_integral_v3cap.csv.

Writes value_integral_compare.csv, one row per wallet of either measurement,
biggest previous share first:
    status      dropped (no allocation any more), reduced, increased
    reason      for dropped wallets: `v1 only` (never in v2 or v3) or
                `v2, never v3`
    old_* new_* days and fraction (share of each measurement's own total
                time: 385 days vs the 357 with something countable)
    v1_usd_at_end, v2_usd_at_end  what the wallet still holds there at the end
                block, i.e. whether it left or could still migrate
    net_pnl_after_entry_exit_usd  from pnl_per_address_usd.csv

Prints the summary and the biggest dropped wallets.

Usage:
    uv run python scripts/compare_value_integrals.py [--top N] [--out FILE]
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

import polars as pl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import value_integral as V  # noqa: E402
from yb import version  # noqa: E402


def holdings_at_end(end_block: int) -> pl.DataFrame:
    """USD value each wallet still holds in v1 and v2 at end_block."""
    w = V.load_window([], None, end_block)
    held: dict[str, dict[str, float]] = defaultdict(lambda: {"v1": 0.0, "v2": 0.0})
    for i in w.ctx:
        ver = version(i)
        if ver not in ("v1", "v2"):
            continue
        for user, recs in w.deltas[i].items():
            _, lt, g = V.trajectory(recs, w)[-1]
            held[user][ver] += lt * w.v_lt[i][-1] + g * w.v_g[i][-1]
    return pl.DataFrame({
        "user": list(held),
        "v1_usd_at_end": [h["v1"] for h in held.values()],
        "v2_usd_at_end": [h["v2"] for h in held.values()],
    })


def main() -> None:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        sys.exit(0)
    top = int(args[args.index("--top") + 1]) if "--top" in args else 20
    output_csv = args[args.index("--out") + 1] if "--out" in args else "value_integral_compare.csv"

    old = pl.read_csv("value_integral.csv")
    new = pl.read_csv("value_integral_v3cap.csv")
    if old["to_block"][0] != new["to_block"][0]:
        raise SystemExit(f"different end blocks: {old['to_block'][0]} vs {new['to_block'][0]}")
    pnl = pl.read_csv("pnl_per_address_usd.csv").select("user", "net_pnl_after_entry_exit_usd")

    df = (old.select("user", pl.col("days").alias("old_days"),
                     pl.col("fraction").alias("old_fraction"),
                     pl.col("markets").cast(pl.String).alias("old_markets"), "hybrid_vault")
             .join(new.select("user", pl.col("days").alias("new_days"),
                              pl.col("fraction").alias("new_fraction"), "days_v2", "days_v3"),
                   on="user", how="full", coalesce=True)
             .join(holdings_at_end(int(old["to_block"][0])), on="user", how="left")
             .join(pnl, on="user", how="left")
             .with_columns(pl.col("old_days", "old_fraction", "new_days", "new_fraction",
                                  "days_v2", "days_v3", "v1_usd_at_end",
                                  "v2_usd_at_end").fill_null(0.0)))

    def in_version(*idx: int) -> pl.Expr:
        return pl.any_horizontal(*[
            pl.col("old_markets").fill_null("").str.split(" ").list.contains(str(i)) for i in idx])

    df = df.with_columns(
        status=pl.when(pl.col("new_days") == 0).then(pl.lit("dropped"))
                 .when(pl.col("new_fraction") < pl.col("old_fraction")).then(pl.lit("reduced"))
                 .otherwise(pl.lit("increased")),
    ).with_columns(
        reason=pl.when(pl.col("status") != "dropped").then(None)
                 .when(in_version(7, 8, 9, 10)).then(pl.lit("UNEXPECTED: held v3"))
                 .when(in_version(3, 4, 5, 6)).then(pl.lit("v2, never v3"))
                 .otherwise(pl.lit("v1 only")),
    ).select(
        "user", "status", "reason", "old_fraction", "new_fraction", "old_days", "new_days",
        "days_v2", "days_v3", "old_markets", "v1_usd_at_end", "v2_usd_at_end",
        "net_pnl_after_entry_exit_usd", "hybrid_vault",
    ).sort("old_fraction", descending=True)
    df.write_csv(output_csv)

    print(f"\n{len(old)} wallets before, {len(new)} now; wrote {len(df)} to {output_csv}\n")
    pl.Config.set_tbl_rows(40)
    pl.Config.set_tbl_cols(10)
    pl.Config.set_fmt_str_lengths(44)
    pl.Config.set_tbl_width_chars(170)
    print(df.group_by("status", "reason")
            .agg(pl.len().alias("wallets"),
                 pl.col("old_fraction").sum().alias("Σ old share"),
                 pl.col("new_fraction").sum().alias("Σ new share"),
                 (pl.col("v1_usd_at_end") + pl.col("v2_usd_at_end")).sum()
                 .alias("still in v1+v2, $"),
                 (pl.col("net_pnl_after_entry_exit_usd") < 0).sum().alias("at a loss"))
            .sort("Σ old share", descending=True))
    print(f"\nBiggest {top} dropped wallets:")
    print(df.filter(pl.col("status") == "dropped").head(top)
            .select("user", "reason", "old_fraction", "old_markets", "v1_usd_at_end",
                    "v2_usd_at_end", "net_pnl_after_entry_exit_usd"))


if __name__ == "__main__":
    main()
