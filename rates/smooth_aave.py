#!/usr/bin/env python3
"""Export a smoothed Aave USDC APR series for reuse as the "market norm".

The raw Aave supply APR is spiky (intraday swings, range ~1.6%–23.5% over
2024–2026), unlike sUSDS. Depositors react to a trailing average, so we publish a
causal **7-day EMA** as the market-rate norm used by the incentive model
(`incentive_sim.py`, which applies the same smoothing internally on its grid).

Time-aware EMA (handles the irregular ~daily sampling):
    alpha_k = 1 - exp(-dt_k / tau),  ema_k = ema_{k-1} + alpha_k*(x_k - ema_{k-1})

Writes `aave_rate_smoothed.csv.xz` (lzma, git-lfs like the other rates data) with
columns:
    timestamp, datetime_utc, aave_usdc_apr (raw), aave_apr_ema7d (smoothed)

Usage
-----
    uv run python smooth_aave.py                 # -> aave_rate_smoothed.csv.xz
    uv run python smooth_aave.py --tau-days 7 --plot
"""
import argparse
import lzma
from pathlib import Path

import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
SRC = HERE / "aave_usdc_rates.csv.xz"
OUT = HERE / "aave_rate_smoothed.csv.xz"
YEAR = 365.25 * 86400.0


def time_aware_ema(t, x, tau_year):
    """Causal EMA over irregular timestamps t (seconds), constant tau_year."""
    out = np.empty_like(x, dtype=np.float64)
    acc = float(x[0])
    out[0] = acc
    for k in range(1, x.size):
        dt_year = (t[k] - t[k - 1]) / YEAR
        a = 1.0 - np.exp(-dt_year / tau_year)
        acc += a * (x[k] - acc)
        out[k] = acc
    return out


def smoothed_aave(tau_days=7.0, src=SRC):
    """Return a polars DataFrame with the raw and EMA-smoothed Aave APR."""
    with lzma.open(src) as fh:
        df = pl.read_csv(fh.read()).sort("timestamp")
    t = df["timestamp"].to_numpy().astype(np.int64)
    apr = df["aave_usdc_apr"].to_numpy().astype(np.float64)
    ema = time_aware_ema(t, apr, tau_days / 365.25)
    return df.select(["timestamp", "datetime_utc", "aave_usdc_apr"]).with_columns(
        pl.Series("aave_apr_ema7d", ema))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tau-days", type=float, default=7.0, help="EMA time constant (days)")
    ap.add_argument("--out", default=str(OUT), help="output CSV path")
    ap.add_argument("--plot", action="store_true", help="show raw vs smoothed")
    args = ap.parse_args()

    df = smoothed_aave(tau_days=args.tau_days)
    with lzma.open(args.out, "wt") as fh:        # lzma, like the other rates data
        fh.write(df.write_csv())
    raw, sm = df["aave_usdc_apr"], df["aave_apr_ema7d"]
    print(f"wrote {args.out}  ({df.height} rows)")
    print(f"raw      apr: min {raw.min()*100:.2f}%  median {raw.median()*100:.2f}%  max {raw.max()*100:.2f}%")
    print(f"smoothed apr: min {sm.min()*100:.2f}%  median {sm.median()*100:.2f}%  max {sm.max()*100:.2f}%")

    if args.plot:
        import matplotlib
        matplotlib.use("QtAgg")
        import matplotlib.pyplot as plt
        tt = df["timestamp"].to_numpy().astype("datetime64[s]")
        fig, ax = plt.subplots(figsize=(12, 5))
        ax.plot(tt, raw.to_numpy() * 100, lw=0.5, color="lightgray", label="Aave raw")
        ax.plot(tt, sm.to_numpy() * 100, lw=1.4, color="steelblue",
                label=f"Aave {args.tau_days:.0f}-day EMA")
        ax.set_ylabel("USDC supply APR [%]"); ax.grid(alpha=0.3); ax.legend()
        ax.set_title("Aave USDC APR — raw vs smoothed market norm")
        fig.tight_layout(); plt.show()


if __name__ == "__main__":
    main()
