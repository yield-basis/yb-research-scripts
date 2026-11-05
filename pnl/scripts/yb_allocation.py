"""YB per wallet, proportional to each time integral. Losses play no part.

Splits YB_TOTAL over wallets in proportion to `days` in two variants:
    full    value_integral.csv: all markets
    v3cap   value_integral_v3cap.csv: v2 + v3 only, v2 capped by what the
            wallet held on average in v3

Outputs, biggest first:
    yb_allocation_full.csv        user, yb, fraction, days, markets, hybrid_vault,
                                  to_block
    yb_allocation_v3cap.csv       the same, plus days_v2 and days_v3
    yb_allocation_difference.csv  user, yb_full, yb_v3cap, yb_difference
                                  (v3cap - full), status (dropped, reduced,
                                  increased), markets, hybrid_vault, to_block

`user` is the wallet to pay: a HybridVault is already merged into its owner,
named in `hybrid_vault`. yb_distribution.py is the older example with a loss
compensation bucket, and is not used here.

Usage:
    uv run python scripts/yb_allocation.py [--total YB]
"""
from __future__ import annotations

import sys

import polars as pl

YB_TOTAL = 1_406_250


def allocate(path: str, total: float, extra: tuple[str, ...] = ()) -> pl.DataFrame:
    df = pl.read_csv(path, schema_overrides={"markets": pl.String})
    return (df.with_columns((pl.col("days") / pl.col("days").sum()).alias("fraction"))
              .with_columns((pl.col("fraction") * total).alias("yb"))
              .select("user", "yb", "fraction", "days", *extra, "markets", "hybrid_vault",
                      "to_block")
              .sort("yb", descending=True))


def main() -> None:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        sys.exit(0)
    total = float(args[args.index("--total") + 1]) if "--total" in args else YB_TOTAL

    full = allocate("value_integral.csv", total)
    capped = allocate("value_integral_v3cap.csv", total, ("days_v2", "days_v3"))
    if full["to_block"][0] != capped["to_block"][0]:
        raise SystemExit(f"different end blocks: {full['to_block'][0]} "
                         f"vs {capped['to_block'][0]}")
    block = full["to_block"][0]

    diff = (full.select("user", pl.col("yb").alias("yb_full"), "markets", "hybrid_vault")
                .join(capped.select("user", pl.col("yb").alias("yb_v3cap")),
                      on="user", how="full", coalesce=True)
                .with_columns(pl.col("yb_full", "yb_v3cap").fill_null(0.0))
                .with_columns((pl.col("yb_v3cap") - pl.col("yb_full")).alias("yb_difference"))
                .with_columns(status=pl.when(pl.col("yb_v3cap") == 0).then(pl.lit("dropped"))
                                       .when(pl.col("yb_difference") < 0).then(pl.lit("reduced"))
                                       .otherwise(pl.lit("increased")))
                .with_columns(pl.lit(block).alias("to_block"))
                .select("user", "yb_full", "yb_v3cap", "yb_difference", "status", "markets",
                        "hybrid_vault", "to_block")
                .sort(pl.col("yb_difference").abs(), descending=True))

    tol = 1e-6
    ok = (abs(full["yb"].sum() - total) < tol and abs(capped["yb"].sum() - total) < tol
          and abs(diff["yb_difference"].sum()) < tol and diff["user"].n_unique() == len(diff))
    if not ok:
        raise SystemExit("amounts do not add up: not writing the CSVs")

    full.write_csv("yb_allocation_full.csv")
    capped.write_csv("yb_allocation_v3cap.csv")
    diff.write_csv("yb_allocation_difference.csv")

    print(f"as of block {block}: {total:,.0f} YB")
    print(f"  full   {len(full):>5} wallets, Σ {full['yb'].sum():,.6f}")
    print(f"  v3cap  {len(capped):>5} wallets, Σ {capped['yb'].sum():,.6f}")
    print(f"  Σ difference {diff['yb_difference'].sum():.6f}\n")
    pl.Config.set_tbl_rows(10)
    pl.Config.set_fmt_str_lengths(44)
    print(diff.group_by("status")
              .agg(pl.len().alias("wallets"), pl.col("yb_full").sum(),
                   pl.col("yb_v3cap").sum(), pl.col("yb_difference").sum())
              .sort("yb_difference"))


if __name__ == "__main__":
    main()
