#!/usr/bin/env python3
"""Is the pyUSD rush inter-pool migration? Short-timescale ΔTVL correlation test.

The supply-sink leakage model (REPORT_incentive_leakage.md) credits the RUSH channel as
100% new liquidity, leaning on the earlier finding of no rush-time anti-correlation.
This makes that test direct and explicit at sub-daily resolution.

Idea: if a pyUSD rush were just crvUSD migrating from the other pools, then over a short
window every $1 that appears in pyUSD must vanish from the others — i.e. regressing
Δothers($) on ΔpyUSD($) would give **slope ≈ −1** (and strong negative correlation), and
the *aggregate* of all crvUSD pools would not move (ΔpyUSD-vs-Δaggregate slope ≈ 0).
Genuinely new liquidity gives Δothers/ΔpyUSD ≈ 0 and Δaggregate/ΔpyUSD ≈ +1. We sweep
the differencing window from one sample (~4 h) out to a week, and isolate the **rush
moments** (top-decile fast pyUSD inflow), where migration — if it happened at the rush —
would show up.

Resolution: the aggregate is re-fetched at pyUSD's ~0.6 h cadence
(crvusd_pools_fine.csv.xz, fetch_crvusd_pools.py --points 10000) so both series share
the same fine sampling — no coarse→fine interpolation artifact (that had inflated the
4 h slope to −0.28; on matched sampling it is ~−0.09).

(The slow ~44% cannibalisation, REPORT_incentive_efficiency.md, was identified from the
dead-band model residual over *weeks*; at long windows the raw ΔΔ correlation is instead
dominated by the common CRV-driven trend, so this short-window test targets the rush.)

Usage: uv run python rush_migration_corr.py --save pics/rush_migration_corr.png
"""
import argparse
import lzma
from pathlib import Path

import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
FINE = HERE / "crvusd_pools_fine.csv.xz"      # ~0.6 h aggregate; falls back to the 4.2 h file
STEP_H = 0.6                                   # uniform resample step (h) ~ pyUSD cadence
WINDOWS_H = [0.6, 1.2, 2.4, 6.0, 12.0, 24.0, 48.0, 96.0, 168.0]
RUSH_ZOOMS = [("2025-10-14", "2025-10-26", "Oct 2025 rush-in"),
              ("2026-01-29", "2026-02-14", "Feb 2026 rush-in")]


def load_uniform():
    src = FINE if FINE.exists() else (HERE / "crvusd_pools.csv.xz")
    with lzma.open(src) as fh:
        c = pl.read_csv(fh.read())
    tc = c["timestamp"].cast(pl.Float64).to_numpy()
    agg = c["staked_tvl"].cast(pl.Float64).to_numpy()
    with lzma.open(HERE / "pool_apr.csv.xz") as fh:
        p = pl.read_csv(fh.read())
    tp = p["timestamp"].cast(pl.Float64).to_numpy()
    py_raw = (p["gauge_staked"].cast(pl.Float64) * p["virtual_price"].cast(pl.Float64)).to_numpy()
    step = float(np.median(np.diff(np.sort(tc)))) / 3600.0
    print(f"aggregate source: {src.name}  ({tc.size:,} pts, ~{step:.2f} h cadence)")
    g = np.arange(tc.min(), tc.max(), STEP_H * 3600.0)   # safe: both series ~0.6 h now
    aggg = np.interp(g, tc, agg)
    pyg = np.interp(g, tp, py_raw)
    return g, pyg, aggg                          # t, pyUSD, aggregate


def stats(dpy, dot):
    if dpy.size < 8 or np.std(dpy) == 0:
        return np.nan, np.nan
    corr = float(np.corrcoef(dpy, dot)[0, 1])
    slope = float(np.cov(dpy, dot)[0, 1] / np.var(dpy))    # Δothers per ΔpyUSD
    return corr, slope


def xcorr(a, b, mask, lags):
    """Cross-correlation function C(τ) = corr(a(t), b(t+τ)) over t where mask holds at
    both t and t+τ. τ>0 ⇒ b lags a (others move AFTER pyUSD)."""
    n = a.size
    out = np.full(len(lags), np.nan)
    for i, L in enumerate(lags):
        if L >= 0:
            x, y, m = a[:n - L], b[L:], mask[:n - L] & mask[L:]
        else:
            x, y, m = a[-L:], b[:n + L], mask[-L:] & mask[:n + L]
        if m.sum() > 30 and x[m].std() > 0 and y[m].std() > 0:
            out[i] = np.corrcoef(x[m], y[m])[0, 1]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()
    t, py, agg = load_uniform()
    oth = agg - py

    print(f"{'window':>8} {'corr(Δpy,Δoth)':>14} {'Δoth/Δpy':>9} {'Δagg/Δpy':>9} "
          f"{'rush Δoth/Δpy':>13} {'rush Δagg/Δpy':>13} {'n_rush':>7}")
    rows = []
    for wh in WINDOWS_H:
        w = max(1, int(round(wh / STEP_H)))
        dpy, dot, dag = py[w:] - py[:-w], oth[w:] - oth[:-w], agg[w:] - agg[:-w]
        corr, slope = stats(dpy, dot)
        _, slope_agg = stats(dpy, dag)
        thr = np.quantile(dpy, 0.90)               # rush = top-decile fast pyUSD inflow
        rmask = dpy >= thr
        _, rslope = stats(dpy[rmask], dot[rmask])
        _, rslope_agg = stats(dpy[rmask], dag[rmask])
        rows.append((wh, corr, slope, slope_agg, rslope, rslope_agg, int(rmask.sum())))
        print(f"{wh:6.1f}h {corr:14.2f} {slope:9.2f} {slope_agg:9.2f} "
              f"{rslope:13.2f} {rslope_agg:13.2f} {int(rmask.sum()):7d}")

    # ---- cross-correlation function C(τ) of one-step (~0.6 h) increments ----
    ipy, ioth, iagg = np.diff(py), np.diff(oth), np.diff(agg)
    tinc = t[1:]
    maxlag = int(round(48.0 / STEP_H))             # ±48 h
    lags = np.arange(-maxlag, maxlag + 1)
    lag_h = lags * STEP_H
    full = np.ones(ipy.size, bool)
    rush = np.zeros(ipy.size, bool)
    ttd = tinc.astype("datetime64[s]")
    for s0, e0, _ in RUSH_ZOOMS:
        rush |= (ttd >= np.datetime64(s0)) & (ttd < np.datetime64(e0))
    C_oth = xcorr(ipy, ioth, full, lags)
    C_agg = xcorr(ipy, iagg, full, lags)
    C_oth_rush = xcorr(ipy, ioth, rush, lags)
    z = lambda L: C_oth[np.argmin(np.abs(lags - L))]
    print(f"cross-corr C(τ)=corr(Δpy(t),Δoth(t+τ)): C(0)={z(0):.2f}  "
          f"C(-1d)={z(-int(round(24/STEP_H))):.2f}  C(+1d)={z(int(round(24/STEP_H))):.2f}  "
          f"min over ±48h={np.nanmin(C_oth):.2f}")

    import matplotlib
    matplotlib.use("Agg" if args.save else "QtAgg")
    import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(13, 11))
    gs = fig.add_gridspec(3, 2, height_ratios=[2.2, 2.2, 2.6])
    axW = fig.add_subplot(gs[0, :])
    axS = fig.add_subplot(gs[1, :])
    azs = [fig.add_subplot(gs[2, i]) for i in range(2)]

    wh = [r[0] for r in rows]
    axW.axhline(0, color="gray", lw=0.8)
    axW.axhline(-1, color="crimson", ls=":", lw=1.0, label="Δoth/Δpy = −1 (pure migration)")
    axW.axhline(1, color="seagreen", ls=":", lw=1.0, label="Δagg/Δpy = +1 (all new money)")
    axW.plot(wh, [r[2] for r in rows], "o-", color="steelblue", label="Δoth/Δpy (all)")
    axW.plot(wh, [r[4] for r in rows], "s--", color="navy", label="Δoth/Δpy (rush)")
    axW.plot(wh, [r[3] for r in rows], "o-", color="seagreen", label="Δagg/Δpy (all)")
    axW.plot(wh, [r[5] for r in rows], "s--", color="darkgreen", label="Δagg/Δpy (rush)")
    axW.set_xscale("log"); axW.set_xlabel("differencing window [h, log]")
    axW.set_ylabel("regression slope")
    axW.set_title("Δ-TVL slopes by timescale — others stay ~0, aggregate ~+1: pyUSD inflow is new money")
    axW.grid(alpha=0.3); axW.legend(fontsize=8, loc="center left", ncol=2)

    axS.axhline(0, color="gray", lw=0.8); axS.axvline(0, color="gray", lw=0.8)
    axS.axhline(-1, color="crimson", ls=":", lw=0.9, label="−1 (pure migration)")
    axS.plot(lag_h, C_oth, color="steelblue", lw=1.4, label="corr(ΔpyUSD(t), Δothers(t+τ)) — full series")
    axS.plot(lag_h, C_agg, color="seagreen", lw=1.4, label="corr(ΔpyUSD(t), Δaggregate(t+τ)) — full series")
    axS.plot(lag_h, C_oth_rush, color="navy", ls="--", lw=1.2, label="Δothers — rush windows only")
    axS.set_xlabel("lag τ [h]   (others/aggregate move τ AFTER pyUSD →)")
    axS.set_ylabel("cross-correlation of ΔTVL")
    axS.set_title("Cross-correlation function C(τ) of ~0.6 h increments — "
                  "no negative lobe near τ=0 (migration would dip to −1)")
    axS.grid(alpha=0.3); axS.legend(fontsize=8, loc="upper right")

    tt = t.astype("datetime64[s]")
    for az, (s0, e0, lab) in zip(azs, RUSH_ZOOMS):
        lo, hi = np.datetime64(s0), np.datetime64(e0)
        seg = (tt >= lo) & (tt < hi)
        az.plot(tt[seg], py[seg] / 1e6, color="crimson", lw=1.6, label="pyUSD TVL")
        az.set_ylabel("pyUSD TVL [$M]", color="crimson")
        az.tick_params(axis="y", labelcolor="crimson")
        az2 = az.twinx()
        az2.plot(tt[seg], oth[seg] / 1e6, color="steelblue", lw=1.6, label="others TVL")
        az2.set_ylabel("others TVL [$M]", color="steelblue")
        az2.tick_params(axis="y", labelcolor="steelblue")
        az.set_title(lab, fontsize=9)
        az.grid(alpha=0.3)
        for l in az.get_xticklabels():
            l.set_rotation(20); l.set_fontsize(7)
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=120); print(f"saved {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
