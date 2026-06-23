#!/usr/bin/env python3
"""Cross-candidate supply-sink test using the simplified analytical plant (p_in = 1).

Drives incentive_sim_pyusd's controller with the analytically-solvable depositor model
(REPORT_pool_dynamics.md "Analytically solvable simplification": fee = 0, p_in = 1 — the
Lambert-W rush). In the normalised (fraction-of-half-TVL) sim the fee term is already
absent, so the only change from the pyUSD calibration is fixing p_in = 1.0.

Procedure:
  1. Optimise the PID once on btc_a5_mf120_of163_... (largest net-pressure excursions,
     incl. the 2024-08-05 crash).
  2. Apply those *fixed* gains to every btc-candidates-yb-opt scenario and record the
     maximum residual net pressure (peak deficit) at 0%, 10%, 20% standing reserve.

Output: incentive_candidates.csv + printed table.

Usage: uv run python incentive_sim_candidates.py
"""
import argparse
import csv
import glob
from pathlib import Path

import incentive_sim_pyusd as B
import incentive_sim_leakage as LK
from incentive_sim_pyusd import CONTROLLERS, build_grid, metrics

HERE = Path(__file__).resolve().parent
# Depositor plant taken ENTIRELY from the simplified analytical fit
# (fit_pool_dynamics_simple.py: fee=0, p_in=1, R² 0.976): activation threshold x_hi,
# and the fitted time constants. We deliberately use the simple fit's own values for
# self-consistency (its band collapsed to [1.52,1.60]; x_hi=1.60 is the inflow edge).
PIN = 1.0
X_HI = 1.60
TAU_IN = 56.7 / 365.25     # years
TAU_OUT = 6.0 / 365.25     # years
BETA = 0.5                 # deposit elasticity — not from the pool fit; default/swept
SCAP = 22.0                # offer cap (APR multiple); high enough to burst-fill the spike
DT_H = 2.0
OPT_KEY = "mf120_of163"   # optimise on btc_a5_mf120_of163_fg00850937_don0187374_rpf433333
RESERVES = [0.0, 0.10, 0.20]
# Peer-cannibalisation efficiency (REPORT_incentive_efficiency / _leakage): the slow
# inflow channel is only ~56% new crvUSD, the rush channel ~100% new. Rush-clean split.
LEAK_EFF = 0.56

B.HYST = X_HI            # override the activation threshold used inside both sims
LK.HYST = X_HI
PLANT = dict(beta=BETA, scap=SCAP, p_in=PIN, tau_in=TAU_IN, tau_out=TAU_OUT,
             leak_eff=LEAK_EFF, rush_clean=True)


def sim_leak(P, m, dty, pl):
    return LK.simulate_leak(P, m, dty, "pid", pl, **PLANT)


def candidate_paths():
    # recursive: catches mf146's dust3600/dust600 sub-runs too (mf137 is .json.xz, a
    # different dump format the npz pipeline doesn't read — excluded).
    return sorted(glob.glob(str(HERE / "btc-candidates-yb-opt" / "**" / "detailed-output.npz.xz"),
                            recursive=True))


def short(path):
    rel = Path(path).relative_to(HERE / "btc-candidates-yb-opt").parent
    return str(rel).replace("btc_a5_", "")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scap", type=float, default=SCAP)
    ap.add_argument("--beta", type=float, default=BETA)
    ap.add_argument("--out", default=str(HERE / "incentive_candidates.csv"))
    args = ap.parse_args()

    PLANT["beta"] = args.beta; PLANT["scap"] = args.scap
    cands = candidate_paths()
    opt_cand = next(c for c in cands if OPT_KEY in c)
    names = CONTROLLERS["pid"][0]

    # 1. optimise PID once on the chosen (worst) candidate, on the leakage-aware plant
    grid, P, m, _, dty = build_grid(opt_cand, dt_hours=DT_H, buffer=0.0)
    pars, J = LK.optimize_leak(P, m, dty, "pid", **PLANT)
    pl = [pars[k] for k in names]
    mt0 = metrics(P, m, dty, sim_leak(P, m, dty, pl), eval_reserve=0.20)
    # no-leakage reference (100% efficiency) for comparison
    nl, _ = B.optimize(P, m, dty, "pid", beta=args.beta, scap=args.scap, p_in=PIN, tau_in=TAU_IN, tau_out=TAU_OUT)
    nl_sim = B.simulate(P, m, dty, "pid", [nl[k] for k in names], beta=args.beta, scap=args.scap,
                        p_in=PIN, tau_in=TAU_IN, tau_out=TAU_OUT)
    nl_spend = metrics(P, m, dty, nl_sim, eval_reserve=0.20)["spend_pa"]
    print(f"plant: simplified-fit (p_in={PIN}, fee=0, x_hi={X_HI}, τin={TAU_IN*365.25:.0f}d, "
          f"τout={TAU_OUT*365.25:.1f}d), beta={args.beta}, scap={args.scap}, dt={DT_H}h")
    print(f"peer-cannibalisation: slow channel {LEAK_EFF*100:.0f}% efficient, rush 100% (rush-clean)")
    print(f"optimised PID on {short(opt_cand)}: "
          + ", ".join(f"{k}={v:.4g}" for k, v in pars.items()))
    print(f"  spend {mt0['spend_pa']*100:.4f}%/yr (×{mt0['spend_pa']/nl_spend:.2f} vs no-leak "
          f"{nl_spend*100:.4f}%/yr), coverage {mt0['coverage']*100:.2f}%, "
          f"offer mean {mt0['mean_x_active']:.2f}x / peak {mt0['peak_x']:.0f}x\n")

    # 2. apply the fixed gains to every candidate; tabulate max residual at each reserve
    hdr = ["candidate", "peak_P%", "spend%/yr", "coverage%"] + [f"maxResid@{int(r*100)}%" for r in RESERVES]
    rows = []
    for c in cands:
        g, P, m, _, dty = build_grid(c, dt_hours=DT_H, buffer=0.0)
        sim = sim_leak(P, m, dty, pl)
        base = metrics(P, m, dty, sim)
        resid = [metrics(P, m, dty, sim, eval_reserve=r)["peak_deficit_res"] * 100 for r in RESERVES]
        rows.append([short(c), P.max() * 100, base["spend_pa"] * 100, base["coverage"] * 100, *resid])

    w = max(len(r[0]) for r in rows)
    print(f"{'candidate':<{w}} {'peakP%':>7} {'spend%':>7} {'cover%':>7} "
          + " ".join(f"resid@{int(r*100)}%".rjust(9) for r in RESERVES))
    for r in rows:
        print(f"{r[0]:<{w}} {r[1]:7.1f} {r[2]:7.4f} {r[3]:7.2f} "
              + " ".join(f"{v:9.2f}" for v in r[4:]))

    with open(args.out, "w", newline="") as fh:
        wr = csv.writer(fh); wr.writerow(hdr)
        for r in rows:
            wr.writerow([r[0]] + [f"{v:.4f}" for v in r[1:]])
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
