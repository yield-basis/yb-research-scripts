#!/usr/bin/env python3
"""Two-population hybrid TVL model: fast jumpers + slow rational LPs.

Adds the rates of two depositor types (same dead band [x_lo, x_hi], x = (fee+rewards/L)/m):

  fast jumpers  — linear response to the *magnitude* of excess APR (no saturation):
        ±κ ·(x − edge)·m
  slow rational — relaxation toward the dead-band equilibrium at a fixed time constant:
        (L*_edge − L)/τ ,   L*_edge = rewards/(edge·m − fee)

So, on the inflow side (x > x_hi):
        dL/dt = κ_in·(x − x_hi)·m  +  (L*_hi − L)/τ_in
and symmetrically below x_lo. The two terms have genuinely different shapes — the fast
term ∝ rewards·(L*−L)/(L·L*) (blows up at small L → the rush), the slow term ∝ (L*−L)
(steady) — so a fit can weight them. Compare R² to the pure forms: linear-abs 0.971
(fit_linear_response.py --absolute), rush/relaxation 0.974 (fit_pool_dynamics.py).

Fitted: κ_in, κ_out, τ_in, τ_out, x_lo, x_hi. Also reports the fraction of total inflow
each population supplied (the empirical fast/slow weight).

Usage: uv run python fit_hybrid.py --save pics/hybrid_fit.png
"""
import argparse

import numpy as np

import fit_pool_dynamics as M

YEAR = M.YEAR
RUSH_WINDOWS = M.RUSH_WINDOWS


def simulate(S, kin, kout, tin_d, tout_d, x_lo, x_hi):
    t, fee, m = S["t"], S["fee"], S["m"]
    rewards = S["crv_val"] + S["yb_val"] + S["yb_lm_val"]
    tin, tout = tin_d / 365.0, tout_d / 365.0
    n = t.size
    L = np.empty(n); L[0] = S["L"][0]; a = np.empty(n)
    fast_in = slow_in = 0.0                                  # accumulated inflow by channel ($)
    for k in range(1, n):
        Lk = L[k - 1]
        apr = fee[k] + rewards[k] / Lk
        a[k] = apr
        x = apr / m[k]
        dt = (t[k] - t[k - 1]) / YEAR
        if x > x_hi:
            Lstar = rewards[k] / max(x_hi * m[k] - fee[k], 1e-9)
            fast = kin * (x - x_hi) * m[k]
            slow = (Lstar - Lk) / tin
            Lk += (fast + slow) * dt
            fast_in += max(fast, 0) * dt; slow_in += max(slow, 0) * dt
        elif x < x_lo:
            Lstar = rewards[k] / max(x_lo * m[k] - fee[k], 1e-9)
            fast = kout * (x - x_lo) * m[k]                  # negative
            slow = (Lstar - Lk) / tout                       # negative if Lk > Lstar
            Lk += (fast + slow) * dt
        L[k] = max(Lk, 1e3)
    a[0] = fee[0] + rewards[0] / L[0]
    return L, a, (fast_in, slow_in)


def loss(S, p):
    L, _, _ = simulate(S, *p)
    e = np.log(L) - np.log(S["L"])
    return float(np.mean(e ** 2))


def fit(S, seed=0):
    from scipy.optimize import differential_evolution
    bounds = [(1e6, 5e11),   # kin  ($/yr per unit (x-x_hi)*m)
              (1e6, 5e11),   # kout
              (0.5, 400.0),  # tau_in (d)
              (0.5, 400.0),  # tau_out (d)
              (0.7, 1.6),    # x_lo
              (1.6, 3.0)]    # x_hi
    r = differential_evolution(lambda p: loss(S, p), bounds, seed=seed,
                               maxiter=90, tol=1e-6, polish=True, updating="deferred")
    return r.x, r.fun


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()
    S = M.load_series()
    p, J = fit(S)
    kin, kout, tin, tout, xlo, xhi = p
    L, a, (fast_in, slow_in) = simulate(S, *p)
    e = np.log(L) - np.log(S["L"])
    r2 = 1.0 - np.sum(e ** 2) / np.sum((np.log(S["L"]) - np.log(S["L"]).mean()) ** 2)
    tot = fast_in + slow_in
    print(f"hybrid fit (log-RMSE {np.sqrt(J):.3f}, R^2 {r2:.3f}):")
    print(f"  kappa_in  = ${kin/1e6:,.0f}M/yr per unit (x-x_hi)*m   tau_in  = {tin:.1f} d")
    print(f"  kappa_out = ${kout/1e6:,.0f}M/yr per unit (x_lo-x)*m   tau_out = {tout:.1f} d")
    print(f"  dead band = [{xlo:.2f}x, {xhi:.2f}x]")
    print(f"  inflow split:  fast jumpers {100*fast_in/tot:.0f}%   slow rational {100*slow_in/tot:.0f}%")

    import matplotlib
    matplotlib.use("Agg" if args.save else "QtAgg")
    import matplotlib.pyplot as plt
    tt = S["t"].astype("datetime64[s]")
    zkeys = [f"z{i}" for i in range(len(RUSH_WINDOWS))]
    fig, axd = plt.subplot_mosaic([["full"] * len(zkeys), ["apr"] * len(zkeys), zkeys],
                                  figsize=(13, 11), height_ratios=[3, 2, 3])
    axL, axA = axd["full"], axd["apr"]; axA.sharex(axL)
    axL.plot(tt, S["L"] / 1e6, lw=1.6, color="black", label="measured staked TVL")
    axL.plot(tt, L / 1e6, lw=1.4, color="crimson", ls="--", label="hybrid model TVL")
    axL.set_yscale("log"); axL.set_ylim(0.5, 100)
    axL.fill_between(tt, 0.5, 100, where=S["yb_on"], color="orange", alpha=0.10, label="YB campaign on")
    axL.set_ylabel("staked TVL [$M, log]")
    axL.set_title(f"Hybrid fast+slow model — fast {100*fast_in/tot:.0f}% / slow {100*slow_in/tot:.0f}% of inflow, "
                  f"τin {tin:.0f}d / τout {tout:.1f}d, band [{xlo:.2f},{xhi:.2f}]×, R² {r2:.3f}")
    axL.legend(loc="upper left", fontsize=8); axL.grid(alpha=0.3)
    axA.plot(tt, a / S["m"], lw=1.0, color="steelblue", label="APR / market (x)")
    axA.axhline(xhi, color="green", ls="--", lw=0.9, label=f"x_hi {xhi:.2f}×")
    axA.axhline(xlo, color="red", ls="--", lw=0.9, label=f"x_lo {xlo:.2f}×")
    axA.set_ylabel("APR / market"); axA.set_yscale("log"); axA.legend(loc="upper right", fontsize=8); axA.grid(alpha=0.3)
    for key, (s0, e0, lab) in zip(zkeys, RUSH_WINDOWS):
        az = axd[key]; lo, hi = np.datetime64(s0), np.datetime64(e0); seg = (tt >= lo) & (tt < hi)
        az.plot(tt[seg], S["L"][seg] / 1e6, lw=1.6, color="black", label="measured")
        az.plot(tt[seg], L[seg] / 1e6, lw=1.6, color="crimson", ls="--", label="model")
        az.set_yscale("log"); az.set_xlim(lo, hi); az.set_title(lab, fontsize=9); az.set_ylabel("TVL [$M, log]")
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
