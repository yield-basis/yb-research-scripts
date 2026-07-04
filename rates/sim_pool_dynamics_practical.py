#!/usr/bin/env python3
"""Practical pool-dynamics under the DEPLOYED controller (6h-filtered derivative).

The companion to fit_pool_dynamics_simple.py: instead of the open-loop depositor fit, this
closes the loop with the real PID.vy — the same simplified plant (fee=0, p_in=1, dead band
[1.52,1.60], tau_in/tau_out) driven by the controller with the deployed 6h Astrom
derivative filter, on the worst BTC net-pressure candidate.  It shows the practical
behaviour that stepwise net pressure + the 6h filter actually produce, and the reason the
derivative filter-time is set to 6h.

Key point: the deployed derivative is low-passed (Tf=6h), which attenuates the raw dP/dt
kick, so the derivative gain Kd must be raised from the raw-tuned 0.0158 to ~0.049 to keep
the ~99% coverage.  Panels:
  (full)   net pressure P vs the effective sink S the 6h-filtered controller attracts;
  (apr)    the offered APR multiple x(t) with the dead band;
  (zoom)   the 2024-08-05 crash — raw vs 6h derivative, same coverage after re-tuning Kd;
  (tf)     the trade-off that picks Tf: reserve spend (rises with Tf) vs offered-APR jitter
           (falls with Tf) at matched coverage — the knee is ~6h.

Usage: uv run python sim_pool_dynamics_practical.py --save pics/pool_dynamics_practical.png
"""
import argparse

import numpy as np

import incentive_sim_pyusd as B
import incentive_sim_leakage as LK
import sim_pid_dfilter as D
from incentive_sim_pyusd import CONTROLLERS, metrics
from sim_reserve_dfilter import register_pidf, PLANT, WORST

YEAR = B.YEAR
SPY = 365 * 86400
B.HYST = 1.60; LK.HYST = 1.60
KD_RAW = 0.0158                      # deployed default (raw-derivative optimum)
KD_6H = 0.049                        # re-tuned for the 6h filter (matched coverage)
GAINS = dict(alpha=1.16, Kp=50.0, Ki=1988.0, Imax=2.93)
TARGET_COV = 0.9913
SINK_NOISE_RATIO = 5.4               # pyUSD sink is 5.4x noisier than real YB net pressure
TF_GRID_H = [0, 1, 3, 6, 12, 24]


def sim_at(P, m, dty, ctrl, kd):
    pl = [GAINS["alpha"], GAINS["Kp"], GAINS["Ki"], kd, GAINS["Imax"]]
    sim = LK.simulate_leak(P, m, dty, ctrl, pl, **PLANT)
    mt = metrics(P, m, dty, sim)
    d = np.clip(P - sim["S"], 0.0, None)
    return sim, mt, d


def kd_for_coverage(P, m, dty, ctrl, target=TARGET_COV):
    lo, hi = 0.003, 0.5
    for _ in range(30):
        mid = (lo + hi) / 2
        _, mt, _ = sim_at(P, m, dty, ctrl, mid)
        if mt["coverage"] < target:
            lo = mid
        else:
            hi = mid
    return hi


def tf_table(P, m, dty):
    """Per-Tf: kd matched to TARGET_COV, reserve spend & tip (BTC crash), and the
    offered-APR jitter the same kd produces on the real per-event pyUSD noise signal."""
    te, ne, _ = D.load_netpressure()
    ng = np.arange(te[0], te[-1], 3600)
    Pn = ne[np.clip(np.searchsorted(te, ng, side="right") - 1, 0, len(ne) - 1)]
    mkt = D.load_market_rate(ng)
    rows = []
    for Tfh in TF_GRID_H:
        register_pidf(Tfh * 3600 / YEAR)
        kd = kd_for_coverage(P, m, dty, "pidf")
        _, mt, d = sim_at(P, m, dty, "pidf", kd)
        d_apr = kd * np.maximum(0.0, D.astrom(Pn, ng, Tfh * 3600)) / 0.5 * mkt * 100
        jit = float(np.std(np.diff(d_apr)))
        rows.append(dict(Tf=Tfh, kd=kd, spend=mt["spend_pa"], cov=mt["coverage"],
                         tip=d.max(), jit_sink=jit, jit_yb=jit / SINK_NOISE_RATIO))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", metavar="PNG")
    ap.add_argument("--candidate", default=WORST)
    args = ap.parse_args()

    grid, P, m, _, dty = B.build_grid(args.candidate, dt_hours=1.0, buffer=0.0)
    register_pidf(6 * 3600 / YEAR)
    sim6, mt6, d6 = sim_at(P, m, dty, "pidf", KD_6H)             # deployed: 6h + retuned Kd
    simr, mtr, dr = sim_at(P, m, dty, "pid", KD_RAW)            # idealised raw derivative
    sim6_lowkd, mt6l, d6l = sim_at(P, m, dty, "pidf", KD_RAW)   # 6h with un-retuned Kd

    print(f"backtest: {P.size:,} steps @1h, peak P {P.max()*100:.1f}%")
    print(f"  raw dP/dt      (Kd={KD_RAW}) : coverage {mtr['coverage']*100:.2f}%  "
          f"spend {mtr['spend_pa']*100:.4f}%/yr  tip {dr.max()*100:.2f}%")
    print(f"  6h filter      (Kd={KD_RAW}) : coverage {mt6l['coverage']*100:.2f}%  "
          f"spend {mt6l['spend_pa']*100:.4f}%/yr  tip {d6l.max()*100:.2f}%   (under-preempts)")
    print(f"  6h filter retun(Kd={KD_6H}) : coverage {mt6['coverage']*100:.2f}%  "
          f"spend {mt6['spend_pa']*100:.4f}%/yr  tip {d6.max()*100:.2f}%   (deployed rec.)")

    print("\nTf trade-off (kd matched to ~99.1% coverage):")
    print(f"{'Tf':>5} {'kd':>7} {'spend%/yr':>10} {'tip%':>6} {'jitYB%':>7} {'jitSink%':>8}")
    tf = tf_table(P, m, dty)
    for r in tf:
        star = "  <-- 6h" if r["Tf"] == 6 else ""
        print(f"{r['Tf']:>4}h {r['kd']:>7.4f} {r['spend']*100:>9.4f}% {r['tip']*100:>5.2f}% "
              f"{r['jit_yb']:>6.2f}% {r['jit_sink']:>7.2f}%{star}")

    if args.save:
        make_figure(grid, P, sim6, mt6, simr, mtr, tf, args.save)


def make_figure(grid, P, sim6, mt6, simr, mtr, tf, save):
    """Same mosaic as fit_pool_dynamics_simple.py: [full]/[apr]/[zoom, closed-form].
    Here the 'plant output' is the controlled sink S (staked crvUSD) instead of the fitted
    pool TVL, and the closed-form panel validates the 6h derivative filter (step -> pulse)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tt = grid.astype("datetime64[s]")
    fig, axd = plt.subplot_mosaic([["full", "full"], ["apr", "apr"], ["zcrash", "zval"]],
                                  figsize=(13, 11), height_ratios=[3, 2, 3])

    # (full) net pressure (the driver, "measured" black) vs the controlled sink (model, red)
    a = axd["full"]
    a.plot(tt, P * 100, lw=0.8, color="black", label="net pressure P (driver)")
    a.plot(tt, sim6["S"] * 100, lw=1.2, color="crimson", ls="--",
           label="controlled sink S — 6h-filtered PID (Kd 0.049)")
    a.fill_between(tt, sim6["S"] * 100, P * 100, where=P > sim6["S"], color="crimson",
                   alpha=0.12, label="uncovered (reserve absorbs)")
    a.set_ylabel("frac of half-TVL [%]")
    a.set_title(f"Practical controlled pool dynamics — 6h filter, τin 57d / τout 6d, "
                f"band [1.52,1.60]×, coverage {mt6['coverage']*100:.1f}%, "
                f"spend {mt6['spend_pa']*100:.3f}%/yr")
    a.legend(loc="upper right", fontsize=8); a.grid(alpha=0.3)

    # (apr) offered APR multiple x(t), log-y, with dead-band edges — mirrors the fit's panel
    b = axd["apr"]; b.sharex(a)
    b.plot(tt, sim6["x"], lw=0.9, color="steelblue", label="offered APR / market (x)")
    b.axhline(1.60, color="green", ls="--", lw=0.9, label="x_hi 1.60× (inflow edge)")
    b.axhline(1.52, color="red", ls="--", lw=0.9, label="x_lo 1.52× (outflow edge)")
    b.set_ylabel("APR / market"); b.set_yscale("log")
    b.legend(loc="upper right", fontsize=8); b.grid(alpha=0.3)

    # (zcrash) 2024-08-05 crash zoom — the sink fills the shoulder (raw vs 6h, re-tuned)
    z = axd["zcrash"]
    lo = np.datetime64("2024-08-02"); hi = np.datetime64("2024-08-12")
    seg = (tt >= lo) & (tt < hi)
    z.plot(tt[seg], P[seg] * 100, lw=1.6, color="black", label="net pressure P")
    z.plot(tt[seg], simr["S"][seg] * 100, lw=1.3, color="#e08000", ls=":",
           label="sink — raw dP/dt (Kd 0.0158)")
    z.plot(tt[seg], sim6["S"][seg] * 100, lw=1.4, color="crimson", ls="--",
           label="sink — 6h filter (Kd 0.049)")
    z.set_ylabel("frac of half-TVL [%]"); z.set_xlim(lo, hi)
    z.set_title("Aug-2024 crash: 6h+re-tuned Kd fills the shoulder like raw", fontsize=9)
    z.legend(fontsize=7, loc="upper right"); z.grid(alpha=0.3)
    for l in z.get_xticklabels():
        l.set_rotation(20); l.set_fontsize(7)

    # (zval) closed-form check of the 6h Astrom filter: a stepwise ΔP -> bounded pulse ~Δ/Tf
    v = axd["zval"]
    Tf_h, dt_h = 6.0, 1.0
    tf_y, dt_y = Tf_h * 3600 / SPY, dt_h * 3600 / SPY
    hrs = np.arange(0, 48, dt_h)
    Pstep = (hrs >= dt_h).astype(float)                     # unit step ΔP=1 at t=0
    d_num = D.astrom(Pstep, hrs * 3600.0, Tf_h * 3600)      # numerical filter (per year)
    ts = hrs                                                # hours since the step
    k = np.maximum(hrs / dt_h, 0)
    d_disc = np.where(hrs >= dt_h, 1.0 / (tf_y + dt_y) * (tf_y / (tf_y + dt_y)) ** (k - 1), 0)
    d_cont = 1.0 / tf_y * np.exp(-(ts) / Tf_h)              # continuous Δ/Tf·e^{-t/Tf}
    v.plot(hrs, d_num, "o", ms=4, color="crimson", alpha=0.6, label="numerical filter")
    v.plot(hrs, d_disc, "-", lw=1.2, color="dodgerblue",
           label="analytic (discrete): Δ/(Tf+dt)·rᵏ")
    v.plot(hrs, d_cont, ":", lw=1.4, color="green", label="continuous: (Δ/Tf)·e^{−t/Tf}")
    v.axhline(1.0 / dt_y, color="gray", ls="--", lw=0.8)
    v.text(24, 1.0 / dt_y, " raw dP/dt spike (Tf=0): Δ/dt ≈ 8760/yr", fontsize=7,
           va="bottom", color="gray")
    v.set_yscale("log"); v.set_xlabel("hours since a stepwise ΔP=1")
    v.set_ylabel("filtered derivative [/yr]")
    v.set_title("6h-filter closed-form: step → bounded pulse ~Δ/Tf, decays over Tf", fontsize=9)
    v.legend(fontsize=7, loc="lower left"); v.grid(alpha=0.3, which="both")

    fig.tight_layout()
    fig.savefig(save, dpi=120)
    print(f"\nsaved {save}")


if __name__ == "__main__":
    main()
