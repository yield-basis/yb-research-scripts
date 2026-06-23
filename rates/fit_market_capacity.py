#!/usr/bin/env python3
"""Alternative TVL-dynamics model: finite market capital × linear excess-APR.

`fit_pool_dynamics.py` uses a relaxation toward an equilibrium with a rush exponent
`p_in` (which turns out degenerate with τ_in and ≈1). This tries a different, arguably
more fundamental law in which the **rush emerges for free** from a linear response
against a **finite pool of addressable market liquidity** `L_market` (a fit parameter):

    inflow  (x > x_hi):  dL/dt = (L_market − L) · (x − x_hi) / τ_in
    hold    (x_lo ≤ x ≤ x_hi):  dL/dt = 0
    outflow (x < x_lo):  dL/dt = − L · (x_lo − x) / τ_out

where `x = (fee + rewards/L) / m` is the pool APR as a multiple of the market rate `m`
(sUSDS), and `[x_lo, x_hi]` is the dead band. Intuition: deposits arrive at a rate
proportional to (a) how much market capital is still *outside* the pool, and (b) how far
the APR sits above the ~2× dead-band edge — exactly the linear excess-APR response
`p_in = 1` implied. A tiny pool has huge `x`, so it fills fast (the rush); the fill
self-limits both as `x` falls (APR dilution) and as `L → L_market` (capital exhaustion).
Outflow drains the pool's own liquidity below the lower edge (more physical than
`(L_market − L)` there; the band/τ_out still set when and how fast).

Fitted: L_market, τ_in, τ_out, x_lo, x_hi. Compare R² to fit_pool_dynamics (0.974).

Usage: uv run python fit_market_capacity.py --save pics/market_capacity_fit.png
"""
import argparse

import numpy as np

import fit_pool_dynamics as M

YEAR = M.YEAR
RUSH_WINDOWS = M.RUSH_WINDOWS


def simulate(S, L_market, tau_in_d, tau_out_d, x_lo, x_hi):
    t, fee, m = S["t"], S["fee"], S["m"]
    rewards = S["crv_val"] + S["yb_val"] + S["yb_lm_val"]
    tin, tout = tau_in_d / 365.0, tau_out_d / 365.0
    n = t.size
    L = np.empty(n); L[0] = S["L"][0]; a = np.empty(n)
    for k in range(1, n):
        Lk = L[k - 1]
        apr = fee[k] + rewards[k] / Lk
        a[k] = apr
        x = apr / m[k]
        dt = (t[k] - t[k - 1]) / YEAR
        if x > x_hi:
            dL = (L_market - Lk) * (x - x_hi) / tin * dt
            Lk += min(dL, max(L_market - Lk, 0.0))      # cap at the remaining headroom
        elif x < x_lo:
            dL = Lk * (x_lo - x) / tout * dt
            Lk -= min(dL, Lk * 0.95)                     # cap so L stays positive
        L[k] = max(Lk, 1e3)
    a[0] = fee[0] + rewards[0] / L[0]
    return L, a


def loss(S, params):
    L, _ = simulate(S, *params)
    e = np.log(L) - np.log(S["L"])
    return float(np.mean(e ** 2))


def fit(S, seed=0):
    from scipy.optimize import differential_evolution
    bounds = [(40e6, 30e9),       # L_market ($)
              (0.2, 400.0),       # tau_in (d)
              (0.5, 60.0),        # tau_out (d)
              (0.7, 1.6),         # x_lo
              (1.6, 3.0)]         # x_hi
    res = differential_evolution(lambda p: loss(S, p), bounds, seed=seed,
                                 maxiter=70, tol=1e-6, polish=True, updating="deferred")
    return res.x, res.fun


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()
    S = M.load_series()
    p, J = fit(S)
    Lm, tin, tout, xlo, xhi = p
    L, a = simulate(S, *p)
    e = np.log(L) - np.log(S["L"])
    r2 = 1.0 - np.sum(e ** 2) / np.sum((np.log(S["L"]) - np.log(S["L"]).mean()) ** 2)
    print(f"market-capacity fit (log-RMSE {np.sqrt(J):.3f}, R^2 {r2:.3f}):")
    print(f"  L_market = ${Lm/1e6:,.0f}M   (pool peaks ~${S['L'].max()/1e6:.0f}M)")
    print(f"  tau_in   = {tin:.1f} d   tau_out = {tout:.1f} d")
    print(f"  dead band = [{xlo:.2f}x, {xhi:.2f}x] market")

    import matplotlib
    matplotlib.use("Agg" if args.save else "QtAgg")
    import matplotlib.pyplot as plt
    tt = S["t"].astype("datetime64[s]")
    zkeys = [f"z{i}" for i in range(len(RUSH_WINDOWS))]
    fig, axd = plt.subplot_mosaic([["full"] * len(zkeys), ["apr"] * len(zkeys), zkeys],
                                  figsize=(13, 11), height_ratios=[3, 2, 3])
    axL, axA = axd["full"], axd["apr"]
    axA.sharex(axL)
    axL.plot(tt, S["L"] / 1e6, lw=1.6, color="black", label="measured staked TVL")
    axL.plot(tt, L / 1e6, lw=1.4, color="crimson", ls="--", label="model TVL")
    axL.set_yscale("log"); axL.set_ylim(0.5, 100)
    axL.fill_between(tt, 0.5, 100, where=S["yb_on"], color="orange", alpha=0.10, label="YB campaign on")
    axL.set_ylabel("staked TVL [$M, log]")
    axL.set_title(f"Market-capacity model — L_market ${Lm/1e6:,.0f}M, τin {tin:.1f}d / τout {tout:.1f}d, "
                  f"band [{xlo:.2f},{xhi:.2f}]×, R² {r2:.3f}")
    axL.legend(loc="upper left", fontsize=8); axL.grid(alpha=0.3)

    axA.plot(tt, a / S["m"], lw=1.0, color="steelblue", label="APR / market (x)")
    axA.axhline(xhi, color="green", ls="--", lw=0.9, label=f"x_hi {xhi:.2f}×")
    axA.axhline(xlo, color="red", ls="--", lw=0.9, label=f"x_lo {xlo:.2f}×")
    axA.set_ylabel("APR / market"); axA.set_yscale("log")
    axA.legend(loc="upper right", fontsize=8); axA.grid(alpha=0.3)

    for key, (s0, e0, lab) in zip(zkeys, RUSH_WINDOWS):
        az = axd[key]
        lo, hi = np.datetime64(s0), np.datetime64(e0)
        seg = (tt >= lo) & (tt < hi)
        az.plot(tt[seg], S["L"][seg] / 1e6, lw=1.6, color="black", label="measured")
        az.plot(tt[seg], L[seg] / 1e6, lw=1.6, color="crimson", ls="--", label="model")
        az.set_yscale("log"); az.set_xlim(lo, hi)
        az.set_title(lab, fontsize=9); az.set_ylabel("TVL [$M, log]")
        az.grid(alpha=0.3, which="both"); az.legend(fontsize=7, loc="upper left")
        for l in az.get_xticklabels():
            l.set_rotation(20); l.set_fontsize(7)
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=120); print(f"saved {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
