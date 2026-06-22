#!/usr/bin/env python3
"""Supply-sink PID, derated for peer cannibalisation (rush stays clean).

`incentive_sim_pyusd.py` sized the scrvUSD/pyUSD supply sink assuming every $ of
attracted TVL absorbs net pressure. But `REPORT_incentive_efficiency.md` measured that
only ~56% of the TVL an incentive parks in pyUSD is *new* crvUSD — the other ~44%
rotates in from other crvUSD pools, which does nothing for net pressure (same crvUSD,
different pool). Crucially that leakage is a **slow, sustained** migration; the **rush**
inflow brought genuinely new liquidity (no rush-time anti-correlation,
`REPORT_crvusd_aggregate.md`). So the two inflow channels should be derated differently.

We split the dead-band inflow rate (1/τ_in)·(x/HYST)^p_in into:
  * BASE / slow channel  — the (x/HYST)^p_in = 1 floor (present even at x = HYST):
        cannibalising, only `leak_eff` (≈0.56) of it is new crvUSD;
  * RUSH channel         — the excess (x/HYST)^p_in − 1 that a high offer adds:
        genuinely new, 100% efficient.

The controller must drive the **effective** (new-liquidity) sink S_eff to cover the
pressure P, but spend is paid on the **whole** pool TVL S (you cannot tell rotated from
new crvUSD, and you pay APR on all of it). So leakage raises the required spend — but a
*bursty* strategy that triggers the clean rush is penalised less than a flat derate.

Scenarios compared (PID re-optimised under each):
  * 100% — no leakage (the original incentive_sim_pyusd assumption);
  * flat 56% — every channel derated to 0.56 (the naive "spend ÷ 0.56");
  * rush-clean — slow channel 0.56, rush channel 100% (the measured structure).

Usage
-----
    uv run python incentive_sim_leakage.py --save pics/incentive_leakage.png
    uv run python incentive_sim_leakage.py --beta 0.5 --scap 40 --buffer 0 --eval-reserve 0.20
"""
import argparse

import numpy as np

import incentive_sim_pyusd as B
from incentive_sim_pyusd import CONTROLLERS, HYST, BETA, SCAP, TAU_IN, TAU_OUT, P_IN, BUFFER


def simulate_leak(P, m, dt_year, ctrl_name, params, beta=BETA, scap=SCAP,
                  tau_in=TAU_IN, tau_out=TAU_OUT, p_in=P_IN,
                  leak_eff=1.0, rush_clean=True):
    """Like incentive_sim_pyusd.simulate, but tracks an EFFECTIVE sink S_eff (the
    new-crvUSD fraction). The controller observes S_eff (it must make the *effective*
    sink cover P); spend is charged on the actual pool TVL S."""
    _, _, step_fn = CONTROLLERS[ctrl_name]
    n = P.size
    S = np.zeros(n); Seff = np.zeros(n); Star = np.zeros(n)
    x = np.zeros(n); iapr = np.zeros(n)
    s = 0.0; seff = 0.0
    state = {"I": 0.0, "dt": dt_year}
    for k in range(n):
        st, state = step_fn(params, P[k], seff, state)     # control on EFFECTIVE sink
        st = min(max(st, 0.0), scap)
        xk = HYST + st / beta
        if st > s:                                         # inflow
            A = (xk / HYST) ** p_in                        # rush amplification (>= 1)
            frac = (dt_year / tau_in) * A
            step = (st - s) * min(frac, 1.0)               # actual TVL added this step
            base_share = 1.0 / A if A > 0 else 1.0         # slow part of the inflow
            if rush_clean:
                eff_add = step * (base_share * leak_eff + (1.0 - base_share) * 1.0)
            else:
                eff_add = step * leak_eff                  # flat derate (rush not spared)
            s += step; seff += eff_add
        else:                                              # outflow — drains proportionally
            step = (st - s) * (dt_year / tau_out)
            if s > 0:
                seff += step * (seff / s)
            s += step
        Star[k] = st; S[k] = s; Seff[k] = max(seff, 0.0)
        if st > 0.0:
            x[k] = xk
            iapr[k] = max(0.0, xk - 1.0) * m[k]
    spend_rate = iapr * S                                  # pay APR on the WHOLE pool
    # B.metrics reads sim["S"] for coverage/deficit and sim["spend_rate"] for spend —
    # feed it S_eff as "S" so coverage is judged on effective (new) liquidity.
    return {"S": Seff, "S_actual": S, "Star": Star, "x": x, "iapr": iapr,
            "spend_rate": spend_rate}


def cost_leak(P, m, dt_year, ctrl_name, params, lam=1.0, **kw):
    return B.metrics(P, m, dt_year, simulate_leak(P, m, dt_year, ctrl_name, params, **kw), lam=lam)["J"]


def optimize_leak(P, m, dt_year, ctrl_name, lam=1.0, seed=0, **kw):
    from scipy.optimize import differential_evolution
    names, bounds, _ = CONTROLLERS[ctrl_name]
    res = differential_evolution(
        lambda pp: cost_leak(P, m, dt_year, ctrl_name, pp, lam=lam, **kw),
        bounds, seed=seed, maxiter=40, tol=1e-4, polish=True, updating="deferred")
    return dict(zip(names, res.x)), res.fun


SCENARIOS = [
    ("100% (no leakage)", dict(leak_eff=1.0, rush_clean=False), "seagreen"),
    ("flat 56%", dict(leak_eff=0.56, rush_clean=False), "darkorange"),
    ("rush-clean (slow 56%, rush 100%)", dict(leak_eff=0.56, rush_clean=True), "crimson"),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate", default=B.WORST)
    ap.add_argument("--controller", default="pid", choices=list(CONTROLLERS))
    ap.add_argument("--dt-hours", type=float, default=4.0)
    ap.add_argument("--beta", type=float, default=BETA)
    ap.add_argument("--scap", type=float, default=40.0, help="offer cap (high → can burst into the rush)")
    ap.add_argument("--buffer", type=float, default=0.0)
    ap.add_argument("--eval-reserve", type=float, default=0.20)
    ap.add_argument("--leak-eff", type=float, default=0.56, help="slow-channel efficiency")
    ap.add_argument("--lam", type=float, default=1.0)
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()

    grid, P, m, m_raw, dt_year = B.build_grid(args.candidate, dt_hours=args.dt_hours, buffer=args.buffer)
    names, _, _ = CONTROLLERS[args.controller]
    print(f"grid {P.size:,} steps  residual pressure mean {P.mean()*100:.3f}%  peak {P.max()*100:.2f}%  "
          f"beta={args.beta} scap={args.scap} buffer={args.buffer} reserve={args.eval_reserve}")
    print(f"{'scenario':36s} {'spend%/yr':>9} {'×base':>6} {'cover%':>7} {'peakDef%':>8} "
          f"{'+reserve':>8} {'meanX':>6} {'peakX':>6}")

    results = []
    base_spend = None
    for label, kw, color in SCENARIOS:
        kw = dict(kw); kw["leak_eff"] = args.leak_eff if kw["leak_eff"] != 1.0 else 1.0
        params, _ = optimize_leak(P, m, dt_year, args.controller, lam=args.lam,
                                  beta=args.beta, scap=args.scap, **kw)
        pl = [params[k] for k in names]
        sim = simulate_leak(P, m, dt_year, args.controller, pl, beta=args.beta, scap=args.scap, **kw)
        mt = B.metrics(P, m, dt_year, sim, lam=args.lam, eval_reserve=args.eval_reserve)
        if base_spend is None:
            base_spend = mt["spend_pa"]
        ratio = mt["spend_pa"] / base_spend if base_spend > 0 else float("nan")
        print(f"{label:36s} {mt['spend_pa']*100:9.4f} {ratio:6.2f} {mt['coverage']*100:7.2f} "
              f"{mt['peak_deficit']*100:8.2f} {mt['peak_deficit_res']*100:8.2f} "
              f"{mt['mean_x_active']:6.2f} {mt['peak_x']:6.1f}")
        results.append((label, color, sim, mt))

    import matplotlib
    matplotlib.use("Agg" if args.save else "QtAgg")
    import matplotlib.pyplot as plt
    tt = grid.astype("datetime64[s]")
    fig, (axS, axX, axB) = plt.subplots(3, 1, figsize=(13, 11))

    axS.plot(tt, P * 100, lw=1.0, color="black", label="net pressure P")
    for label, color, sim, mt in results:
        axS.plot(tt, sim["S"] * 100, lw=1.0, color=color, alpha=0.9,
                 label=f"effective sink — {label} (cov {mt['coverage']*100:.1f}%)")
    axS.set_ylabel("frac of half-TVL [%]"); axS.grid(alpha=0.3); axS.legend(fontsize=7, loc="upper right")
    axS.set_title("Effective (new-liquidity) sink vs net pressure — PID re-optimised per leakage model")
    axS.sharex(axX)

    for label, color, sim, mt in results:
        axX.plot(tt, sim["x"], lw=0.8, color=color, alpha=0.8, label=f"offered x — {label}")
    axX.axhline(HYST, color="k", ls="--", lw=0.8, label=f"{HYST:.2f}× dead-band")
    axX.set_ylabel("offered APR / market (x)"); axX.set_yscale("log")
    axX.grid(alpha=0.3); axX.legend(fontsize=7, loc="upper right")

    labels = [r[0] for r in results]
    spends = [r[3]["spend_pa"] * 100 for r in results]
    colors = [r[1] for r in results]
    axB.bar(range(len(labels)), spends, color=colors, alpha=0.8)
    for i, s in enumerate(spends):
        axB.text(i, s, f"{s:.3f}%\n×{s/spends[0]:.2f}", ha="center", va="bottom", fontsize=8)
    axB.set_xticks(range(len(labels))); axB.set_xticklabels(labels, fontsize=8)
    axB.set_ylabel("spend [%/yr of half-TVL]"); axB.grid(alpha=0.3, axis="y")
    axB.set_title("Required spend by leakage model (×relative to no-leakage)")
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=120); print(f"saved {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
