#!/usr/bin/env python3
"""Simplified pool-dynamics fit (fee = 0, p_in = 1) — the analytically solvable case.

The full model (fit_pool_dynamics.py) has a trading-fee term and a free rush exponent
p_in. Two simplifications make it analytically tractable with negligible loss of fit:

  * fee = 0  — trading fee (~0.26%) is small vs the ~2× dead band;
  * p_in = 1 — the fitted value (1.03) is degenerate with τ_in and ≈ 1 anyway.

Model (state L = staked TVL; x = APR/market = rewards/(L·m); dead band [x_lo, x_hi]):

  inflow  (x > x_hi):  dL/dt = (L*_hi − L)/τ_in · (x/x_hi),   L*_hi = rewards/(x_hi·m)
  outflow (x < x_lo):  dL/dt = (L*_lo − L)/τ_out,             L*_lo = rewards/(x_lo·m)
  hold otherwise

With fee=0 and p_in=1, using x/x_hi = L*_hi/L the inflow reduces (per constant-reward
epoch, u = L/L*) to the separable  du/dt = (1−u)·u^{-1}/τ_in, whose exact solution is

  t/τ_in = ln((1−u0)/(1−u)) − (u − u0)      ⇔      L(t) = L*·[1 + W₀(−e^{−T})],
  T = t/τ_in + (1−u0) − ln(1−u0)

(W₀ = principal Lambert-W). Limits: early L ∝ √t (the rush), late 1−u ∝ e^{−t/τ_in} (the
plain exponential). The fit below integrates the ODE numerically against the real
(time-varying) reward series; the rush-zoom panels overlay the closed form (with locally
constant rewards) to confirm it.

Usage: uv run python fit_pool_dynamics_simple.py --save pics/pool_dynamics_simple_fit.png
"""
import argparse

import numpy as np
from scipy.special import lambertw

import fit_pool_dynamics as M

YEAR = M.YEAR
RUSH_WINDOWS = M.RUSH_WINDOWS


def rewards_of(S):
    return S["crv_val"] + S["yb_val"] + S["yb_lm_val"]


def simulate(S, tau_in_d, tau_out_d, x_lo, x_hi):
    t, m = S["t"], S["m"]
    rewards = rewards_of(S)
    tin, tout = tau_in_d / 365.0, tau_out_d / 365.0
    n = t.size
    L = np.empty(n); L[0] = S["L"][0]; a = np.empty(n)
    for k in range(1, n):
        Lk = L[k - 1]
        apr = rewards[k] / Lk                              # fee = 0
        a[k] = apr
        x = apr / m[k]
        dt = (t[k] - t[k - 1]) / YEAR
        if x > x_hi:
            Lstar = rewards[k] / (x_hi * m[k])
            Lk += (Lstar - Lk) * (x / x_hi) * dt / tin     # p_in = 1
        elif x < x_lo:
            Lstar = rewards[k] / (x_lo * m[k])
            Lk += (Lstar - Lk) * dt / tout
        L[k] = max(Lk, 1e3)
    a[0] = rewards[0] / L[0]
    return L, a


def analytic_inflow(t_rel_yr, u0, L_star, tin_yr):
    """Closed-form inflow L(t) (constant rewards): L = L*[1 + W0(-e^{-T})]."""
    T = t_rel_yr / tin_yr + (1.0 - u0) - np.log(1.0 - u0)
    u = 1.0 + np.real(lambertw(-np.exp(-T), 0))
    return u * L_star


def loss(S, p):
    L, _ = simulate(S, *p)
    e = np.log(L) - np.log(S["L"])
    return float(np.mean(e ** 2))


def fit(S, seed=0):
    from scipy.optimize import differential_evolution
    bounds = [(1.0, 60.0), (1.0, 60.0), (0.7, 1.6), (1.6, 3.0)]   # tau_in, tau_out, x_lo, x_hi
    r = differential_evolution(lambda p: loss(S, p), bounds, seed=seed,
                               maxiter=80, tol=1e-6, polish=True, updating="deferred")
    return r.x, r.fun


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()
    S = M.load_series()
    p, J = fit(S)
    tin, tout, xlo, xhi = p
    L, a = simulate(S, *p)
    e = np.log(L) - np.log(S["L"])
    r2 = 1.0 - np.sum(e ** 2) / np.sum((np.log(S["L"]) - np.log(S["L"]).mean()) ** 2)
    print(f"simplified fit (fee=0, p_in=1) — log-RMSE {np.sqrt(J):.3f}, R^2 {r2:.3f}:")
    print(f"  tau_in {tin:.1f} d   tau_out {tout:.1f} d   dead band [{xlo:.2f}, {xhi:.2f}]x")
    print(f"  (full model for reference: R^2 0.974, band [1.50, 2.14], tau_out 5.9 d)")

    import matplotlib
    matplotlib.use("Agg" if args.save else "QtAgg")
    import matplotlib.pyplot as plt
    tt = S["t"].astype("datetime64[s]")
    rewards = rewards_of(S)
    tin_yr = tin / 365.0
    fig, axd = plt.subplot_mosaic([["full", "full"], ["apr", "apr"], ["zreal", "zval"]],
                                  figsize=(13, 11), height_ratios=[3, 2, 3])
    axL, axA = axd["full"], axd["apr"]; axA.sharex(axL)
    axL.plot(tt, S["L"] / 1e6, lw=1.6, color="black", label="measured staked TVL")
    axL.plot(tt, L / 1e6, lw=1.4, color="crimson", ls="--", label="simplified model (numerical)")
    axL.set_yscale("log"); axL.set_ylim(0.5, 100)
    axL.fill_between(tt, 0.5, 100, where=S["yb_on"], color="orange", alpha=0.10, label="YB campaign on")
    axL.set_ylabel("staked TVL [$M, log]")
    axL.set_title(f"Simplified pool dynamics (fee=0, p_in=1) — τin {tin:.0f}d / τout {tout:.0f}d, "
                  f"band [{xlo:.2f},{xhi:.2f}]×, R² {r2:.3f}")
    axL.legend(loc="upper left", fontsize=8); axL.grid(alpha=0.3)
    axA.plot(tt, a / S["m"], lw=1.0, color="steelblue", label="APR / market (x)")
    axA.axhline(xhi, color="green", ls="--", lw=0.9, label=f"x_hi {xhi:.2f}×")
    axA.axhline(xlo, color="red", ls="--", lw=0.9, label=f"x_lo {xlo:.2f}×")
    axA.set_ylabel("APR / market"); axA.set_yscale("log"); axA.legend(loc="upper right", fontsize=8); axA.grid(alpha=0.3)

    # left: real Feb-2026 rush zoom (measured vs numerical)
    s0, e0, lab = RUSH_WINDOWS[-1]
    az = axd["zreal"]; lo, hi = np.datetime64(s0), np.datetime64(e0); seg = (tt >= lo) & (tt < hi)
    az.plot(tt[seg], S["L"][seg] / 1e6, lw=1.6, color="black", label="measured")
    az.plot(tt[seg], L[seg] / 1e6, lw=1.4, color="crimson", ls="--", label="numerical model")
    az.set_yscale("log"); az.set_xlim(lo, hi); az.set_title(lab + " (real)", fontsize=9)
    az.set_ylabel("TVL [$M, log]"); az.grid(alpha=0.3, which="both"); az.legend(fontsize=7, loc="upper left")
    for l in az.get_xticklabels():
        l.set_rotation(20); l.set_fontsize(7)

    # right: closed-form validation on a CONSTANT-reward step (analytic must equal numerical)
    av = axd["zval"]
    R = float(np.median(rewards[S["yb_on"]])); mc = float(np.median(S["m"]))
    Lstar = R / (xhi * mc); L0 = 0.01 * Lstar; u0 = L0 / Lstar
    days = np.linspace(1e-3, 4 * tin, 4000); ty = days / 365.0
    # numerical integration of the simplified inflow ODE, constant R, mc
    Ln = np.empty(days.size); Ln[0] = L0
    for i in range(1, days.size):
        x = (R / Ln[i - 1]) / mc
        dLn = (Lstar - Ln[i - 1]) * (x / xhi) / tin_yr * (ty[i] - ty[i - 1]) if x > xhi else 0.0
        Ln[i] = Ln[i - 1] + dLn
    La = analytic_inflow(ty, u0, Lstar, tin_yr)
    av.plot(days, Ln / Lstar, lw=2.6, color="crimson", alpha=0.5, label="numerical ODE")
    av.plot(days, La / Lstar, lw=1.2, color="dodgerblue", ls="--", label="analytic: 1+W₀(−e^−T)")
    av.plot(days, np.sqrt(2 * ty / tin_yr), lw=1.0, color="green", ls=":", label="early: √(2t/τ_in)")
    av.set_xscale("log"); av.set_yscale("log"); av.set_ylim(u0, 1.3)
    av.set_xlabel("days since rush start"); av.set_ylabel("u = L / L*")
    av.set_title("Closed-form check (constant rewards): analytic ≡ numerical; √t rush → plateau", fontsize=9)
    av.grid(alpha=0.3, which="both"); av.legend(fontsize=7, loc="lower right")
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=120); print(f"saved {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
