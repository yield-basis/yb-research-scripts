#!/usr/bin/env python3
"""Symmetric linear-response TVL model — the most identifiable form.

Deposits/withdrawals flow at a rate **linear in how far the pool APR sits past the
dead-band edge**, with the rate set by a single constant on each side (no time constant,
no rush exponent, no capital ceiling — all of which turned out degenerate, see
REPORT_pool_dynamics.md / the p_in and L_market explorations):

    inflow  (x > x_hi):  dL/dt = κ_in  · (x − x_hi)
    hold    (x_lo ≤ x ≤ x_hi):  dL/dt = 0
    outflow (x < x_lo):  dL/dt = κ_out · (x − x_lo)        (negative)

where `x = (fee + rewards/L)/m` is the pool APR as a multiple of the market rate `m`
(sUSDS). The rush emerges for free (tiny pool ⇒ huge x ⇒ fast fill, self-limiting as the
endogenous APR dilutes). Both κ's are clean, fully-determined rate constants ($/yr per
unit of excess x); κ_out/κ_in is the fast-out/slow-in asymmetry as one number.

Note: outflow is *marginally* better described as size-proportional (∝ L, the §3
exponential wind-down, R² ~0.972) than this flat κ_out form (R² ~0.966); both are
defensible. This script uses the symmetric constant-rate form for interpretability.

Usage: uv run python fit_linear_response.py --save pics/linear_response_fit.png
"""
import argparse

import numpy as np

import fit_pool_dynamics as M

YEAR = M.YEAR
RUSH_WINDOWS = M.RUSH_WINDOWS


def simulate(S, kin, kout, x_lo, x_hi, absolute=False):
    """absolute=False: rate ∝ ratio excess (x − edge); True: ∝ absolute APR excess
    (x − edge)·m = (APR − edge·m). The latter fits marginally better but is hard to
    distinguish on this data (m only ranges 3.5–4.6%)."""
    t, fee, m = S["t"], S["fee"], S["m"]
    rewards = S["crv_val"] + S["yb_val"] + S["yb_lm_val"]
    n = t.size
    L = np.empty(n); L[0] = S["L"][0]; a = np.empty(n)
    for k in range(1, n):
        Lk = L[k - 1]
        apr = fee[k] + rewards[k] / Lk
        a[k] = apr
        x = apr / m[k]
        w = m[k] if absolute else 1.0
        dt = (t[k] - t[k - 1]) / YEAR
        if x > x_hi:
            Lk += kin * (x - x_hi) * w * dt
        elif x < x_lo:
            Lk -= min(kout * (x_lo - x) * w * dt, Lk * 0.95)
        L[k] = max(Lk, 1e3)
    a[0] = fee[0] + rewards[0] / L[0]
    return L, a


def loss(S, p, absolute=False):
    L, _ = simulate(S, *p, absolute=absolute)
    e = np.log(L) - np.log(S["L"])
    return float(np.mean(e ** 2))


def fit(S, seed=0, absolute=False):
    from scipy.optimize import differential_evolution
    hi = 5e11 if absolute else 5e9
    bounds = [(1e6, hi), (1e6, hi), (0.7, 1.6), (1.6, 3.0)]   # kin, kout, x_lo, x_hi
    r = differential_evolution(lambda p: loss(S, p, absolute), bounds, seed=seed,
                               maxiter=80, tol=1e-6, polish=True, updating="deferred")
    return r.x, r.fun


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--absolute", action="store_true",
                    help="rate ∝ (APR − edge·m) absolute excess, instead of the ratio (x − edge)")
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()
    S = M.load_series()
    p, J = fit(S, absolute=args.absolute)
    kin, kout, xlo, xhi = p
    L, a = simulate(S, *p, absolute=args.absolute)
    e = np.log(L) - np.log(S["L"])
    r2 = 1.0 - np.sum(e ** 2) / np.sum((np.log(S["L"]) - np.log(S["L"]).mean()) ** 2)
    unit = "(APR − edge·m)" if args.absolute else "(x − edge)"
    mode = "absolute" if args.absolute else "ratio"
    print(f"symmetric linear-response fit [{mode}] (log-RMSE {np.sqrt(J):.3f}, R^2 {r2:.3f}):")
    print(f"  kappa_in  = ${kin/1e6:,.0f}M/yr per unit {unit}")
    print(f"  kappa_out = ${kout/1e6:,.0f}M/yr per unit {unit}   (out/in = {kout/kin:.1f}x)")
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
    axL.set_title(f"Symmetric linear-response [{mode}] — κ_in ${kin/1e6:,.0f}M/yr, κ_out ${kout/1e6:,.0f}M/yr "
                  f"(out/in {kout/kin:.1f}×), band [{xlo:.2f},{xhi:.2f}]×, R² {r2:.3f}")
    axL.legend(loc="upper left", fontsize=8); axL.grid(alpha=0.3)

    axA.plot(tt, a / S["m"], lw=1.0, color="steelblue", label="APR / market (x)")
    axA.axhline(xhi, color="green", ls="--", lw=0.9, label=f"x_hi {xhi:.2f}× (inflow edge)")
    axA.axhline(xlo, color="red", ls="--", lw=0.9, label=f"x_lo {xlo:.2f}× (outflow edge)")
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
