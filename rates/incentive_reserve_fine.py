#!/usr/bin/env python3
"""Fine-resolution reserve analysis — the true instantaneous residual tip.

The cross-candidate spreadsheet (incentive_sim_candidates.py) runs on a 2-hour grid,
which smooths away sub-hour flash crashes and so *understates* the instantaneous residual.
This re-optimises the PID at a fine grid (default 15 min) on the worst candidate and
reports the trustworthy worst-case: the max residual tip, how briefly it lasts, the BTC
flash that drives it, and what a 1/2/3/5/8% standing reserve actually leaves uncovered.

Same simplified analytical plant as the spreadsheet (fee=0, p_in=1, x_hi=1.60, τ from the
fit) with the rush-clean leakage. Key finding: the residual is set by the *rate* of
pressure rise (dP/dt), not the peak level — a 30-min flash leaves a far bigger gap than a
gradual grind to a higher peak.

Usage: uv run python incentive_reserve_fine.py --dt-hours 0.25
"""
import argparse
import datetime as dt

import numpy as np

import incentive_sim_pyusd as B
import incentive_sim_leakage as LK
from incentive_sim_pyusd import CONTROLLERS, build_grid, metrics
from net_pressure import load_npz_xz, PRICE_KEY

# simplified analytical plant, taken from fit_pool_dynamics_simple.py (self-consistent)
X_HI = 1.60
B.HYST = X_HI; LK.HYST = X_HI
PLANT = dict(beta=0.5, scap=22.0, p_in=1.0, tau_in=56.7 / 365.25, tau_out=6.0 / 365.25,
             leak_eff=0.56, rush_clean=True)
WORST = ("btc-candidates-yb-opt/btc_a5_mf120_of163_fg00850937_don0187374_rpf433333/"
         "detailed-output.npz.xz")
RESERVES = [0.0, 0.01, 0.02, 0.03, 0.05, 0.08]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dt-hours", type=float, default=0.25)
    ap.add_argument("--candidate", default=WORST)
    args = ap.parse_args()

    names = CONTROLLERS["pid"][0]
    grid, P, m, _, dty = build_grid(args.candidate, dt_hours=args.dt_hours, buffer=0.0)
    print(f"{args.dt_hours*60:.0f}-min grid: {P.size:,} steps, peak net pressure {P.max()*100:.1f}%", flush=True)
    pars, _ = LK.optimize_leak(P, m, dty, "pid", **PLANT)
    pl = [pars[k] for k in names]
    sim = LK.simulate_leak(P, m, dty, "pid", pl, **PLANT)
    mt = metrics(P, m, dty, sim)
    d = np.clip(P - sim["S"], 0.0, None)          # 0-reserve residual
    print("optimised:", ", ".join(f"{k}={v:.4g}" for k, v in pars.items()))
    print(f"  coverage {mt['coverage']*100:.2f}%  spend {mt['spend_pa']*100:.4f}%/yr  "
          f"max residual {d.max()*100:.2f}%")
    hrs = lambda thr: float(np.sum(d > thr) * args.dt_hours)
    print(f"  hours above:  >0.5% {hrs(0.005):.1f}   >1% {hrs(0.01):.1f}   "
          f">2% {hrs(0.02):.1f}   >5% {hrs(0.05):.1f}")

    print("  reserve → uncovered:")
    for r in RESERVES:
        dr = np.clip(d - r, 0.0, None)
        print(f"    {r*100:4.0f}%  max {dr.max()*100:5.2f}%  ({hrs(r):.1f} h still uncovered)")

    imax = int(np.argmax(d)); i0 = imax
    while i0 > 0 and d[i0] > 0:
        i0 -= 1
    dd = load_npz_xz(args.candidate); o = np.argsort(dd["t"].astype(np.int64))
    price = np.interp(grid, dd["t"].astype(np.int64)[o], dd[PRICE_KEY].astype(np.float64)[o])
    f = lambda i: dt.datetime.fromtimestamp(int(grid[i]), dt.UTC).strftime("%Y-%m-%d %H:%M")
    print(f"  worst tip: {f(i0)} (P={P[i0]*100:.0f}%, BTC ${price[i0]:,.0f}) → "
          f"{f(imax)} (P={P[imax]*100:.0f}%, BTC ${price[imax]:,.0f}); "
          f"BTC {(price[imax]-price[i0])/price[i0]*100:+.1f}% over {(grid[imax]-grid[i0])/3600:.1f} h")


if __name__ == "__main__":
    main()
