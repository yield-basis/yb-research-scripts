#!/usr/bin/env python3
"""Supply-sink incentive sim driven by the SYMMETRIC LINEAR-RESPONSE plant.

Replaces the dead-band-relaxation+rush depositor model (incentive_sim_pyusd.py) with the
measured linear-response law (fit_linear_response.py --absolute, R² 0.971, the best fit):

    dS/dt = κ_in ·(x − x_hi)·m   when x > x_hi    (inflow,  absolute excess APR)
    dS/dt = κ_out·(x − x_lo)·m   when x < x_lo    (outflow, negative)
    hold                          in the dead band

x = offered scrvUSD APR as a multiple of the market rate m; the controller sets x. The
defining difference from the old model: capital flows at a **constant rate** set by the
offer (no relaxation toward a finite depth, no τ, no rush exponent), so the sink can be
filled to any level given a high enough offer — coverage is **rate-limited, never
depth-limited**. The rush is automatic (high x ⇒ high flow). All in fractions of half-TVL.

Calibration (fit_linear_response.py --absolute):
  band [x_lo, x_hi] = [1.36×, 2.56×];  κ_out/κ_in ≈ 2.66 (leave 2.7× faster than arrive).
  κ_in is the inflow responsiveness in (fraction-of-half-TVL)/yr per unit (x−x_hi) at the
  typical market rate. Anchored to the pyUSD pool: κ_abs ≈ $286M/yr per unit (x−x_hi);
  normalised by the pool's ~$68M scale → κ ≈ 4/yr. SCALE-FREE only if the addressable
  crvUSD market grows with the protected system (same caveat as the old β); hence swept.

Usage
-----
    uv run python incentive_sim_linresp.py --optimize --save pics/incentive_linresp.png
    uv run python incentive_sim_linresp.py --sweep-kappa
"""
import argparse

import numpy as np

import incentive_sim_pyusd as B
from incentive_sim_pyusd import CONTROLLERS, build_grid, metrics

X_LO, X_HI = 1.36, 2.56            # absolute linear-response fit
KOUT_RATIO = 21148.0 / 7954.0      # κ_out/κ_in ≈ 2.66
MBAR = 0.039                       # median market rate used in the absolute-model normalisation
KAPPA = 4.0                        # inflow responsiveness, frac-half-TVL/yr per unit (x−x_hi) at m=MBAR
XMAX = 40.0                        # offer cap (APR multiple)


def simulate(P, m, dt_year, ctrl_name, params, kappa=KAPPA, x_lo=X_LO, x_hi=X_HI,
             kout_ratio=KOUT_RATIO, x_max=XMAX, mbar=MBAR):
    step_fn = CONTROLLERS[ctrl_name][2]
    n = P.size
    S = np.zeros(n); Star = np.zeros(n); x = np.zeros(n); iapr = np.zeros(n)
    s = 0.0
    state = {"I": 0.0, "dt": dt_year}
    for k in range(n):
        raw, state = step_fn(params, P[k], s, state)        # controller output (offer excess above x_lo)
        xk = min(max(x_lo + raw, 1.0), x_max)               # offered APR multiple (anchored at the cheap hold edge)
        w = m[k] / mbar                                     # absolute-model market-rate scaling
        if xk > x_hi:
            s += kappa * (xk - x_hi) * w * dt_year          # inflow
        elif xk < x_lo:
            s = max(s + kappa * kout_ratio * (xk - x_lo) * w * dt_year, 0.0)   # outflow
        s = max(s, 0.0)
        S[k] = s
        Star[k] = xk - x_hi                                  # >0 ⇒ inflow active (for metrics)
        if xk > 1.0:
            x[k] = xk
            iapr[k] = (xk - 1.0) * m[k]                      # bonus APR above 1× base
    return {"S": S, "Star": Star, "x": x, "iapr": iapr, "spend_rate": iapr * S}


def cost(P, m, dt_year, ctrl_name, params, lam=1.0, **kw):
    return metrics(P, m, dt_year, simulate(P, m, dt_year, ctrl_name, params, **kw), lam=lam)["J"]


def optimize(P, m, dt_year, ctrl_name, lam=1.0, seed=0, **kw):
    from scipy.optimize import differential_evolution
    names, bounds, _ = CONTROLLERS[ctrl_name]
    res = differential_evolution(lambda pp: cost(P, m, dt_year, ctrl_name, pp, lam=lam, **kw),
                                 bounds, seed=seed, maxiter=40, tol=1e-4, polish=True, updating="deferred")
    return dict(zip(names, res.x)), res.fun


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate", default=B.WORST)
    ap.add_argument("--controller", default="pid", choices=list(CONTROLLERS))
    ap.add_argument("--dt-hours", type=float, default=2.0)
    ap.add_argument("--buffer", type=float, default=0.0)
    ap.add_argument("--eval-reserve", type=float, default=0.20)
    ap.add_argument("--kappa", type=float, default=KAPPA)
    ap.add_argument("--xmax", type=float, default=XMAX)
    ap.add_argument("--lam", type=float, default=1.0)
    ap.add_argument("--sweep-kappa", action="store_true")
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()

    grid, P, m, m_raw, dt_year = build_grid(args.candidate, dt_hours=args.dt_hours, buffer=args.buffer)
    names = CONTROLLERS[args.controller][0]
    print(f"grid {P.size:,} steps  net pressure mean {P.mean()*100:.3f}%  peak {P.max()*100:.2f}%  "
          f"band [{X_LO},{X_HI}]x  kout/kin {KOUT_RATIO:.2f}")

    if args.sweep_kappa:
        print(f"{'kappa /yr':>9} {'spend%/yr':>9} {'cover%':>7} {'peakDef%':>8} {'+20%res':>8} {'meanX':>6} {'peakX':>6}")
        for kp in [1.0, 2.0, 4.0, 8.0, 16.0]:
            pars, _ = optimize(P, m, dt_year, args.controller, lam=args.lam, kappa=kp, x_max=args.xmax)
            pl = [pars[k] for k in names]
            sim = simulate(P, m, dt_year, args.controller, pl, kappa=kp, x_max=args.xmax)
            mt = metrics(P, m, dt_year, sim, lam=args.lam, eval_reserve=args.eval_reserve)
            print(f"{kp:9.1f} {mt['spend_pa']*100:9.4f} {mt['coverage']*100:7.2f} {mt['peak_deficit']*100:8.2f} "
                  f"{mt['peak_deficit_res']*100:8.2f} {mt['mean_x_active']:6.2f} {mt['peak_x']:6.1f}")
        return

    pars, J = optimize(P, m, dt_year, args.controller, lam=args.lam, kappa=args.kappa, x_max=args.xmax)
    pl = [pars[k] for k in names]
    print("optimised:", {k: round(v, 4) for k, v in pars.items()})
    sim = simulate(P, m, dt_year, args.controller, pl, kappa=args.kappa, x_max=args.xmax)
    mt = metrics(P, m, dt_year, sim, lam=args.lam, eval_reserve=args.eval_reserve)
    print(f"kappa={args.kappa}/yr  coverage {mt['coverage']*100:.2f}%  spend {mt['spend_pa']*100:.4f}%/yr  "
          f"peak deficit {mt['peak_deficit']*100:.2f}%  with 20% reserve {mt['peak_deficit_res']*100:.2f}%")
    print(f"offer: mean {mt['mean_x_active']:.2f}x active, peak {mt['peak_x']:.1f}x, active {mt['frac_active']*100:.1f}% of time")

    import matplotlib
    matplotlib.use("Agg" if args.save else "QtAgg")
    import matplotlib.pyplot as plt
    tt = grid.astype("datetime64[s]")
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(13, 10), sharex=True)
    ax1.plot(tt, P * 100, lw=0.8, color="crimson", label="net pressure P")
    ax1.plot(tt, sim["S"] * 100, lw=1.0, color="steelblue", label="sink S (linear-response)")
    ax1.fill_between(tt, sim["S"] * 100, P * 100, where=P > sim["S"], color="crimson", alpha=0.15, label="uncovered")
    ax1.set_ylabel("frac of half-TVL [%]"); ax1.grid(alpha=0.3); ax1.legend(fontsize=8, loc="upper right")
    ax1.set_title(f"Linear-response supply sink (κ={args.kappa}/yr) — coverage {mt['coverage']*100:.1f}%, "
                  f"spend {mt['spend_pa']*100:.3f}%/yr of half-TVL")
    ax2.plot(tt, sim["x"], lw=0.8, color="purple", label="offered APR / market (x)")
    ax2.axhline(X_HI, color="green", ls="--", lw=0.8, label=f"x_hi {X_HI}× (inflow)")
    ax2.axhline(X_LO, color="red", ls="--", lw=0.8, label=f"x_lo {X_LO}× (outflow)")
    ax2.set_ylabel("APR multiple"); ax2.set_yscale("log"); ax2.grid(alpha=0.3); ax2.legend(fontsize=8, loc="upper right")
    ax3.plot(tt, sim["iapr"] * 100, lw=0.8, color="darkorange", label="incentive (bonus) APR")
    ax3.plot(tt, m * 100, lw=1.0, color="gray", label="market norm (Aave, smoothed)")
    ax3.set_ylabel("APR [%]"); ax3.grid(alpha=0.3); ax3.legend(fontsize=8, loc="upper right")
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=120); print(f"saved {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
