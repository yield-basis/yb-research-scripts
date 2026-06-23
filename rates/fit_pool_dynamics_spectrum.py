#!/usr/bin/env python3
"""Multi-timescale ("rich") pool-dynamics fit — a spectrum of depositor speeds.

The "rich" τ-distribution case (each speed-class has its OWN capital and its OWN current
fill, so the headroom stays inside the integral → a true superposition of exponentials).
This is the multi-timescale generalisation of the fee=0 relaxation model **with no rush
exponent** — the spread of time constants supplies the fast fill instead of a p_in>1.

State: K log-spaced τ-buckets, capital weight w(τ) = log-normal in ln τ (median τ_med,
width σ; σ→0 recovers a single τ). Each bucket relaxes toward its share of the dead-band
equilibrium at its own rate; the pool APR is endogenous on the total:

  x = rewards/(L·m),  L = Σ L_i      (fee = 0)
  x > x_hi : dL_i/dt = (w_i·L*_hi − L_i)/τ_i ,  L*_hi = rewards/(x_hi·m)   (inflow)
  x < x_lo : dL_i/dt = (w_i·L*_lo − L_i)/τ_i ,  L*_lo = rewards/(x_lo·m)   (outflow)
  else     : hold

Fitted: τ_med, σ, x_lo, x_hi (4 params, same count as the base fit). Compare R² to
single-τ no-rush (≈0.970) and the rush model (0.974): if the spectrum reaches ~0.974 it
reproduces the rush as a timescale spread (fast buckets) rather than a high-APR exponent.

Usage: uv run python fit_pool_dynamics_spectrum.py --save pics/pool_dynamics_spectrum_fit.png
"""
import argparse

import numpy as np

import fit_pool_dynamics as M

YEAR = M.YEAR
RUSH_WINDOWS = M.RUSH_WINDOWS
TAUS_D = np.geomspace(0.3, 300.0, 24)          # τ-grid, days (fast jumpers → very slow)
TAUS_Y = TAUS_D / 365.0


def weights(tau_med, sigma):
    lw = np.exp(-((np.log(TAUS_D) - np.log(tau_med)) ** 2) / (2.0 * sigma ** 2))
    return lw / lw.sum()


def simulate(S, tau_med, sigma, x_lo, x_hi):
    t, m = S["t"], S["m"]
    rewards = S["crv_val"] + S["yb_val"] + S["yb_lm_val"]
    w = weights(tau_med, sigma)
    n = t.size
    Li = w * S["L"][0]                          # initial split across buckets
    Ltot = np.empty(n); Ltot[0] = S["L"][0]; a = np.empty(n)
    for k in range(1, n):
        Lk = Li.sum()
        apr = rewards[k] / Lk                   # fee = 0
        a[k] = apr
        x = apr / m[k]
        dt = (t[k] - t[k - 1]) / YEAR
        if x > x_hi:
            target = w * (rewards[k] / (x_hi * m[k]))
            Li += (target - Li) * np.minimum(dt / TAUS_Y, 1.0)
        elif x < x_lo:
            target = w * (rewards[k] / (x_lo * m[k]))
            Li += (target - Li) * np.minimum(dt / TAUS_Y, 1.0)
        Li = np.maximum(Li, 1.0)
        Ltot[k] = Li.sum()
    a[0] = rewards[0] / Ltot[0]
    return Ltot, a, w


def loss(S, p):
    L, _, _ = simulate(S, *p)
    e = np.log(L) - np.log(S["L"])
    return float(np.mean(e ** 2))


def fit(S, seed=0):
    from scipy.optimize import differential_evolution
    bounds = [(1.0, 120.0),   # tau_med (d)
              (0.05, 3.0),    # sigma (log-tau width; ->0 = single tau)
              (0.7, 1.6),     # x_lo
              (1.6, 3.0)]     # x_hi
    r = differential_evolution(lambda p: loss(S, p), bounds, seed=seed,
                               maxiter=80, tol=1e-6, polish=True, updating="deferred")
    return r.x, r.fun


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()
    S = M.load_series()
    p, J = fit(S)
    tau_med, sigma, xlo, xhi = p
    L, a, w = simulate(S, *p)
    e = np.log(L) - np.log(S["L"])
    r2 = 1.0 - np.sum(e ** 2) / np.sum((np.log(S["L"]) - np.log(S["L"]).mean()) ** 2)
    # effective spread in decades, and the τ range holding the central 80% of capital
    cw = np.cumsum(w)
    tlo = TAUS_D[np.searchsorted(cw, 0.1)]; thi = TAUS_D[min(np.searchsorted(cw, 0.9), len(TAUS_D) - 1)]
    print(f"spectrum fit (fee=0, multi-τ) — log-RMSE {np.sqrt(J):.3f}, R^2 {r2:.3f}:")
    print(f"  tau_med {tau_med:.1f} d   sigma {sigma:.2f} (log-τ width)   band [{xlo:.2f}, {xhi:.2f}]x")
    print(f"  central 80% of capital spans τ ≈ {tlo:.1f}–{thi:.0f} d  ({np.log10(thi/tlo):.1f} decades)")
    print(f"  (single-τ no-rush ≈ 0.970; rush model 0.974)")

    import matplotlib
    matplotlib.use("Agg" if args.save else "QtAgg")
    import matplotlib.pyplot as plt
    tt = S["t"].astype("datetime64[s]")
    fig, axd = plt.subplot_mosaic([["full", "full"], ["apr", "apr"], ["spec", "zoom"]],
                                  figsize=(13, 11), height_ratios=[3, 2, 3])
    axL, axA = axd["full"], axd["apr"]; axA.sharex(axL)
    axL.plot(tt, S["L"] / 1e6, lw=1.6, color="black", label="measured staked TVL")
    axL.plot(tt, L / 1e6, lw=1.4, color="crimson", ls="--", label="spectrum model")
    axL.set_yscale("log"); axL.set_ylim(0.5, 100)
    axL.fill_between(tt, 0.5, 100, where=S["yb_on"], color="orange", alpha=0.10, label="YB campaign on")
    axL.set_ylabel("staked TVL [$M, log]")
    axL.set_title(f"Multi-timescale spectrum (fee=0, no rush exponent) — τ_med {tau_med:.0f}d, "
                  f"σ {sigma:.2f}, band [{xlo:.2f},{xhi:.2f}]×, R² {r2:.3f}")
    axL.legend(loc="upper left", fontsize=8); axL.grid(alpha=0.3)
    axA.plot(tt, a / S["m"], lw=1.0, color="steelblue", label="APR / market (x)")
    axA.axhline(xhi, color="green", ls="--", lw=0.9, label=f"x_hi {xhi:.2f}×")
    axA.axhline(xlo, color="red", ls="--", lw=0.9, label=f"x_lo {xlo:.2f}×")
    axA.set_ylabel("APR / market"); axA.set_yscale("log"); axA.legend(loc="upper right", fontsize=8); axA.grid(alpha=0.3)

    axS = axd["spec"]
    axS.bar(np.log10(TAUS_D), w, width=0.9 * np.diff(np.log10(TAUS_D)).mean(), color="slateblue", alpha=0.7)
    axS.set_xlabel("log₁₀ τ [days]"); axS.set_ylabel("capital weight w(τ)")
    axS.set_title("Fitted speed spectrum (capital share by time constant)", fontsize=9); axS.grid(alpha=0.3)

    s0, e0, lab = RUSH_WINDOWS[-1]
    az = axd["zoom"]; lo, hi = np.datetime64(s0), np.datetime64(e0); seg = (tt >= lo) & (tt < hi)
    az.plot(tt[seg], S["L"][seg] / 1e6, lw=1.6, color="black", label="measured")
    az.plot(tt[seg], L[seg] / 1e6, lw=1.4, color="crimson", ls="--", label="spectrum model")
    az.set_yscale("log"); az.set_xlim(lo, hi); az.set_title(lab + " (real)", fontsize=9)
    az.set_ylabel("TVL [$M, log]"); az.grid(alpha=0.3, which="both"); az.legend(fontsize=7, loc="upper left")
    for l in az.get_xticklabels():
        l.set_rotation(20); l.set_fontsize(7)
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=120); print(f"saved {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
