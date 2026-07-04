#!/usr/bin/env python3
"""Does the real 6h derivative filter change the reserve/spend sizing?

incentive_reserve_fine.py sizes the standing reserve by driving the leakage PID on the
worst BTC candidate and taking the residual tip d = max(0, P - S_eff). Its `_pid_step`
uses the IDEALISED raw derivative dP/dt = (P[k]-P[k-1])/dt — which, on a fine grid, reacts
instantly and grid-dependently. The deployed PID.vy instead low-passes the derivative with
a 6h Astrom filter (dt & Tf in years):

    d[k] = (Tf*d[k-1] + (P[k]-P[k-1])) / (Tf + dt) ,   target += Kd*max(0, d[k])

This script reruns the SAME reserve analysis with a 6h-filtered derivative and compares,
answering: is the expected spend and the reserve tip the same under realistic smoothing?

Two views:
  * re-optimised — each derivative model gets its own best gains (best achievable);
  * matched-gains — the raw-optimised gains applied under both filters (pure filter effect);
  * grid sweep — spend & tip vs grid dt for raw vs 6h (the raw derivative is grid-dependent;
    the Astrom filter is dt-invariant, i.e. robust to the real trigger cadence).

Usage: uv run python sim_reserve_dfilter.py [--dt-hours 0.25] [--save pics/reserve_dfilter.png]
"""
import argparse

import numpy as np

import incentive_sim_pyusd as B
import incentive_sim_leakage as LK
from incentive_sim_pyusd import CONTROLLERS, metrics

YEAR = B.YEAR
X_HI = 1.60
B.HYST = X_HI; LK.HYST = X_HI          # match incentive_reserve_fine's dead-band edge
PLANT = dict(beta=0.5, scap=22.0, p_in=1.0, tau_in=56.7 / 365.25, tau_out=6.0 / 365.25,
             leak_eff=0.56, rush_clean=True)
WORST = ("btc-candidates-yb-opt/btc_a5_mf120_of163_fg00850937_don0187374_rpf433333/"
         "detailed-output.npz.xz")
RESERVES = [0.0, 0.01, 0.02, 0.03, 0.05, 0.08]


def register_pidf(tf_years):
    """(Re)register a PID whose derivative is the 6h Astrom filter, same gains/bounds."""
    def step(p, Pk, S, state):
        alpha, Kp, Ki, Kd, Imax = p
        I = min(max(state["I"] + (Pk - S) * state["dt"], 0.0), Imax)
        state["I"] = I
        dp = Pk - state.get("prevP", Pk)
        dt = state["dt"]
        d = (tf_years * state.get("dfilt", 0.0) + dp) / (tf_years + dt)
        state["dfilt"] = d
        state["prevP"] = Pk
        return alpha * Pk + Kp * (Pk - S) + Ki * I + Kd * max(0.0, d), state
    CONTROLLERS["pidf"] = (CONTROLLERS["pid"][0], CONTROLLERS["pid"][1], step)


def reserve_row(P, dty, sim, dt_hours):
    d = np.clip(P - sim["S"], 0.0, None)         # sim["S"] is the effective sink
    hrs = lambda thr: float(np.sum(d > thr) * dt_hours)
    tbl = {r: (float(np.clip(d - r, 0.0, None).max()), hrs(r)) for r in RESERVES}
    return d, tbl


def run_model(P, m, dty, ctrl, dt_hours, params=None):
    names = CONTROLLERS[ctrl][0]
    if params is None:
        pars, _ = LK.optimize_leak(P, m, dty, ctrl, **PLANT)
        params = pars
    pl = [params[k] for k in names]
    sim = LK.simulate_leak(P, m, dty, ctrl, pl, **PLANT)
    mt = metrics(P, m, dty, sim)
    d, tbl = reserve_row(P, dty, sim, dt_hours)
    return params, sim, mt, d, tbl


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dt-hours", type=float, default=0.25)
    ap.add_argument("--candidate", default=WORST)
    ap.add_argument("--tf-hours", type=float, default=6.0, help="d_filter_time (default 6h)")
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()

    tf_years = args.tf_hours * 3600 / YEAR
    register_pidf(tf_years)
    grid, P, m, _, dty = B.build_grid(args.candidate, dt_hours=args.dt_hours, buffer=0.0)
    print(f"{args.dt_hours*60:.0f}-min grid: {P.size:,} steps, peak P {P.max()*100:.1f}%, "
          f"Tf={args.tf_hours:g}h\n")

    def show(tag, params, mt, d, tbl):
        print(f"[{tag}]  " + ", ".join(f"{k}={v:.4g}" for k, v in params.items()))
        print(f"   coverage {mt['coverage']*100:.2f}%   spend {mt['spend_pa']*100:.4f}%/yr   "
              f"max residual {d.max()*100:.2f}%")
        print("   reserve→max-uncovered: " +
              "  ".join(f"{int(r*100)}%:{tbl[r][0]*100:.2f}%" for r in RESERVES))

    # 1) re-optimised: best achievable under each derivative model
    print("=== RE-OPTIMISED (each derivative model gets its own best gains) ===")
    p_raw, s_raw, m_raw, d_raw, t_raw = run_model(P, m, dty, "pid", args.dt_hours)
    show("raw  dP/dt (idealised)", p_raw, m_raw, d_raw, t_raw)
    p_f, s_f, m_f, d_f, t_f = run_model(P, m, dty, "pidf", args.dt_hours)
    show(f"6h Astrom filter (real)", p_f, m_f, d_f, t_f)
    print(f"   Δspend {(m_f['spend_pa']-m_raw['spend_pa'])*100:+.4f}%/yr   "
          f"Δmax-residual {(d_f.max()-d_raw.max())*100:+.2f}%")

    # 2) matched gains: raw-optimised gains, applied under both filters (pure filter effect)
    print("\n=== MATCHED GAINS (raw-optimised gains under both derivatives) ===")
    _, _, mm_raw, dd_raw, tt_raw = run_model(P, m, dty, "pid", args.dt_hours, params=p_raw)
    _, _, mm_f, dd_f, tt_f = run_model(P, m, dty, "pidf", args.dt_hours, params=p_raw)
    show("raw", p_raw, mm_raw, dd_raw, tt_raw)
    show("6h filter (same gains)", p_raw, mm_f, dd_f, tt_f)

    # 3) grid sweep with FIXED (15-min-raw-optimised) gains: the raw derivative's
    # response scales with the grid dt (spikier as dt shrinks) while the Astrom filter
    # is dt-invariant — i.e. robust to the real, irregular trigger cadence.
    print("\n=== GRID DEPENDENCE (fixed raw-optimised gains): spend & reserve tip ===")
    print(f"{'dt':>6} | {'raw spend':>10} {'raw tip':>8} | {'6h spend':>10} {'6h tip':>8}")
    sweep = []
    for dh in [4.0, 2.0, 1.0, 0.5, 0.25]:
        g, Pg, mg, _, dyg = B.build_grid(args.candidate, dt_hours=dh, buffer=0.0)
        _, _, mr, dr, _ = run_model(Pg, mg, dyg, "pid", dh, params=p_raw)
        _, _, mf, df, _ = run_model(Pg, mg, dyg, "pidf", dh, params=p_raw)
        sweep.append((dh, mr["spend_pa"], dr.max(), mf["spend_pa"], df.max()))
        print(f"{dh:>5.2f}h | {mr['spend_pa']*100:>9.4f}% {dr.max()*100:>7.2f}% | "
              f"{mf['spend_pa']*100:>9.4f}% {df.max()*100:>7.2f}%")

    if args.save:
        make_figure(grid, P, s_raw, s_f, d_raw, d_f, sweep, args.dt_hours, args.tf_hours, args.save)


def make_figure(grid, P, s_raw, s_f, d_raw, d_f, sweep, dt_hours, tf_hours, save):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tt = grid.astype("datetime64[s]")
    imax = int(np.argmax(np.maximum(d_raw, d_f)))
    w = int(round(10 * 86400 / (dt_hours * 3600)))
    lo, hi = max(0, imax - w), min(len(grid), imax + w)

    fig, ax = plt.subplot_mosaic([["z", "z"], ["res", "sweep"]], figsize=(14, 9),
                                 height_ratios=[3, 3])
    a = ax["z"]
    a.plot(tt[lo:hi], P[lo:hi] * 100, lw=1.2, color="black", label="net pressure P")
    a.plot(tt[lo:hi], s_raw["S"][lo:hi] * 100, lw=1.1, color="#e53e3e",
           label="effective sink — raw dP/dt")
    a.plot(tt[lo:hi], s_f["S"][lo:hi] * 100, lw=1.1, color="#2f855a",
           label=f"effective sink — {tf_hours:g}h filter")
    a.fill_between(tt[lo:hi], s_f["S"][lo:hi] * 100, P[lo:hi] * 100,
                   where=P[lo:hi] > s_f["S"][lo:hi], color="#2f855a", alpha=0.12)
    a.set_ylabel("frac of half-TVL [%]")
    a.set_title(f"Worst reserve tip (10-day zoom): raw vs {tf_hours:g}h-filtered derivative")
    a.legend(fontsize=8, loc="upper left"); a.grid(alpha=0.3)

    r = ax["res"]
    r.plot(tt[lo:hi], d_raw[lo:hi] * 100, lw=1.0, color="#e53e3e", label="residual — raw")
    r.plot(tt[lo:hi], d_f[lo:hi] * 100, lw=1.0, color="#2f855a", label=f"residual — {tf_hours:g}h")
    r.set_ylabel("uncovered residual [% half-TVL]")
    r.set_title("Residual tip the reserve must cover")
    r.legend(fontsize=8); r.grid(alpha=0.3)

    s = ax["sweep"]
    dh = [x[0] for x in sweep]
    s.plot(dh, [x[1] * 100 for x in sweep], "o-", color="#e53e3e", label="raw: spend %/yr")
    s.plot(dh, [x[3] * 100 for x in sweep], "o-", color="#2f855a", label=f"{tf_hours:g}h: spend %/yr")
    s.set_xlabel("simulation grid dt [h]"); s.set_ylabel("spend [%/yr]")
    s.set_title("Grid dependence: raw derivative drifts with dt; filter is stable")
    s.set_xscale("log"); s.set_xticks(dh); s.set_xticklabels([f"{x:g}" for x in dh])
    s.legend(fontsize=8, loc="center left"); s.grid(alpha=0.3, which="both")
    st = s.twinx()
    st.plot(dh, [x[2] * 100 for x in sweep], "s--", color="#e53e3e", alpha=0.6)
    st.plot(dh, [x[4] * 100 for x in sweep], "s--", color="#2f855a", alpha=0.6)
    st.set_ylabel("max residual tip [%]  (dashed)")

    fig.tight_layout()
    fig.savefig(save, dpi=120)
    print(f"\nsaved {save}")


if __name__ == "__main__":
    main()
