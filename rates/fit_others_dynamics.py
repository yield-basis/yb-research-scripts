#!/usr/bin/env python3
"""Dead-band TVL fit for the NON-pyUSD crvUSD pools ("others" = aggregate − pyUSD).

`crvusd_others.png` showed pyUSD inflows do not pull capital out of the other crvUSD
pools (no rush-time anti-correlation). But Jan–Jun 2026 is less obvious: the other
pools' incentive fell for months AND their TVL bled out over the same window. Two
candidate causes:

  (A) their own incentive dropped (CRV price ~halved) → the dead-band model says TVL
      must relax down to a lower equilibrium; OR
  (B) the freshly-incentivised pyUSD pool competed capital away.

This fits the SAME endogenous dead-band relaxation model used for pyUSD
(`fit_pool_dynamics.py`) to the *others* series, driven ONLY by the others' own
incentive (CRV + on-gauge YB; the non-pyUSD pYB was a voter bribe, not an LP reward —
its effect is already inside CRV, see REPORT_crvusd_aggregate.md). If that
incentive-only model reproduces the Feb–May decline, the outflow is (A) — explained by
the incentive drop, not pyUSD competition. The model residual over Feb–May is the
test: ≈0 ⇒ no unexplained outflow ⇒ no need for a competition term.

Model (state L = others staked TVL, $):
    rewards(t) = CRV_value_others(t) + on-gauge YB_others(t)         [$/yr]
    APR a      = rewards / L            (aggregate fee APR ≈ 0, omitted)
    x          = a / m(t)              m = sUSDS market rate
    x>x_hi: inflow to rewards/(x_hi·m), rate (1/τ_in)·(x/x_hi)^p_in;  x<x_lo: outflow to
    rewards/(x_lo·m), rate 1/τ_out;  else hold.  Fitted: τ_in, τ_out, x_lo, x_hi; the
    rush exponent p_in is GRAFTED from the pyUSD pool (1.03), not re-fit here.

Usage
-----
    uv run python fit_others_dynamics.py --save pics/others_dynamics_fit.png
"""
import argparse
import datetime as dt
import lzma
from pathlib import Path

import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
POOLS_CSV = HERE / "crvusd_pools.csv.xz"
APR_CSV = HERE / "pool_apr.csv.xz"
SUSDS_CSV = HERE / "susds_rates.csv.xz"
YEAR = 365.0 * 86400.0
# pyUSD's hooked April LP campaign (the only pYB that reached pyUSD LPs); on-gauge YB
# windows come from yb_period_finish in pool_apr.csv.
PY_LM_START, PY_LM_END = "2026-04-16", "2026-04-30"
# Rush exponent measured on the pyUSD pool (fit_pool_dynamics.py): inflow accelerates
# as (x/x_hi)^p_in above the band edge. We GRAFT it here rather than re-fitting it from
# the noisy aggregate — the rush is a depositor-behaviour constant, not pool-specific.
P_IN = 1.03


def _ts(s):
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC).timestamp())


def _intervals(t, mask, merge_gap_d=5.0):
    """Contiguous [start, end] runs of True in `mask`, merging gaps < merge_gap_d days."""
    iv = []
    in_run = False
    for k in range(t.size):
        if mask[k] and not in_run:
            s = t[k]; in_run = True
        elif not mask[k] and in_run:
            iv.append([s, t[k - 1]]); in_run = False
    if in_run:
        iv.append([s, t[-1]])
    merged = []
    for s, e in iv:
        if merged and s - merged[-1][1] < merge_gap_d * 86400:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return merged


def load_series():
    with lzma.open(POOLS_CSV) as fh:
        c = pl.read_csv(fh.read())
    g = lambda col: c[col].cast(pl.Float64, strict=False).to_numpy()
    t = g("timestamp")
    agg = g("staked_tvl")
    crv_rate, sum_rw, crv_px = g("crv_rate"), g("sum_rel_weight"), g("crv_price")
    yb_sum_rate, yb_px = g("yb_sum_rate"), g("yb_price")

    with lzma.open(APR_CSV) as fh:
        p = pl.read_csv(fh.read())
    gp = lambda col: p[col].cast(pl.Float64, strict=False).to_numpy()
    tp = gp("timestamp")
    py_tvl = np.interp(t, tp, gp("gauge_staked") * gp("virtual_price"))
    py_rw = np.interp(t, tp, gp("crv_rel_weight"))
    py_yb_on = np.where(tp < gp("yb_period_finish"), gp("yb_rate") * gp("yb_price") * YEAR, 0.0)
    py_yb_on = np.interp(t, tp, py_yb_on)
    # pyUSD "being incentivised" mask: on-gauge YB active OR the April hooked LP campaign
    py_on_g = np.interp(t, tp, (tp < gp("yb_period_finish")).astype(float)) > 0.5
    py_on = py_on_g | ((t >= _ts(PY_LM_START)) & (t < _ts(PY_LM_END)))

    crv_others = crv_rate * np.clip(sum_rw - py_rw, 0, None) * crv_px * YEAR
    yb_others = np.clip(yb_sum_rate * yb_px * YEAR - py_yb_on, 0, None)   # ≈0
    rewards = crv_others + yb_others
    L = np.clip(agg - py_tvl, 1e3, None)

    with lzma.open(SUSDS_CSV) as fh:
        s = pl.read_csv(fh.read())
    m = np.interp(t, s["timestamp"].to_numpy(), s["susds_apr"].to_numpy())

    # pyUSD APR (for the competition diagnostic panel)
    py_rewards = crv_rate * py_rw * crv_px * YEAR + py_yb_on
    py_apr = py_rewards / np.clip(py_tvl, 1e3, None)
    return dict(t=t, L=L, rewards=rewards, crv_others=crv_others, m=m,
                py_tvl=py_tvl, py_apr=py_apr, others_apr=rewards / L, py_on=py_on)


def simulate(S, tau_in_d, tau_out_d, x_lo, x_hi):
    t, rewards, m = S["t"], S["rewards"], S["m"]
    tau_in, tau_out = tau_in_d / 365.0, tau_out_d / 365.0
    n = t.size
    L = np.empty(n); L[0] = S["L"][0]; a = np.empty(n)
    for k in range(1, n):
        Lk = L[k - 1]
        apr = rewards[k] / Lk
        a[k] = apr
        x = apr / m[k]
        dtk = (t[k] - t[k - 1]) / YEAR
        if x > x_hi:
            denom = x_hi * m[k]
            Lt = rewards[k] / denom if denom > 1e-6 else Lk * 4
            frac = (dtk / tau_in) * (x / x_hi) ** P_IN     # rush (pyUSD-measured exponent)
            Lk = Lk + (Lt - Lk) * min(frac, 1.0)
        elif x < x_lo:
            denom = x_lo * m[k]
            Lt = rewards[k] / denom if denom > 1e-6 else Lk * 0.25
            Lk = Lk + (Lt - Lk) * (dtk / tau_out)
        L[k] = max(Lk, 1e3)
    a[0] = rewards[0] / L[0]
    return L, a


def loss(S, params):
    L, _ = simulate(S, *params)
    e = np.log(L) - np.log(S["L"])
    return float(np.mean(e ** 2))


def fit(S, seed=0):
    from scipy.optimize import differential_evolution
    bounds = [(1.0, 60.0), (1.0, 120.0), (0.7, 1.6), (1.6, 3.5)]   # tin, tout, xlo, xhi
    res = differential_evolution(lambda pp: loss(S, pp), bounds, seed=seed,
                                 maxiter=80, tol=1e-6, polish=True, updating="deferred")
    return res.x, res.fun


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()

    S = load_series()
    params, J = fit(S)
    tin, tout, xlo, xhi = params
    L, a = simulate(S, *params)
    e = np.log(L) - np.log(S["L"])
    r2 = 1.0 - np.sum(e ** 2) / np.sum((np.log(S["L"]) - np.log(S["L"]).mean()) ** 2)

    tt = S["t"].astype("datetime64[s]")
    py_on = S["py_on"]
    bands = _intervals(S["t"], py_on)
    # the 2026 pyUSD-incentivised window (Jan31–Apr30) for the decline metric
    y26 = tt >= np.datetime64("2026-01-01")
    on26 = py_on & y26
    seg = (S["t"] >= S["t"][on26][0]) & (S["t"] <= S["t"][on26][-1])
    resid = (L[seg] - S["L"][seg]) / S["L"][seg]      # (model − measured)/measured
    drop_meas = S["L"][seg][-1] / S["L"][seg][0] - 1
    drop_mod = L[seg][-1] / L[seg][0] - 1
    # incentive elasticity over Jan–Jun
    jj = y26 & (tt <= np.datetime64("2026-06-15"))
    ll, lr = np.log(S["L"][jj]), np.log(S["rewards"][jj])
    elast = np.polyfit(lr, ll, 1)[0]
    corr_LR = np.corrcoef(lr, ll)[0, 1]
    # competition: others' outflow speed while pyUSD is incentivised vs not (2026)
    dLfrac = np.gradient(S["L"]) / S["L"]
    rate_on = dLfrac[y26 & py_on].mean() * 365
    rate_off = dLfrac[y26 & ~py_on].mean() * 365
    corr_comp = np.corrcoef(dLfrac[jj], (S["py_apr"] - S["others_apr"])[jj])[0, 1]
    print(f"fit (log-RMSE {np.sqrt(J):.3f}, R^2 {r2:.3f}):")
    print(f"  tau_in {tin:.1f} d  tau_out {tout:.1f} d  band [{xlo:.2f}, {xhi:.2f}]×  "
          f"p_in {P_IN:.2f} (grafted from pyUSD)")
    print(f"  pyUSD-incentivised 2026 window: measured {drop_meas*100:+.0f}%  model "
          f"{drop_mod*100:+.0f}%  mean resid {resid.mean()*100:+.1f}% (model−measured)")
    print(f"  incentive: corr(lnL,lnR) {corr_LR:.2f}  elasticity dlnL/dlnR {elast:.2f}")
    print(f"  competition: others outflow {rate_on*100:+.0f}%/yr while pyUSD ON vs "
          f"{rate_off*100:+.0f}%/yr OFF (2026)  corr(dTVL,ΔAPR) {corr_comp:+.2f}")

    import matplotlib
    matplotlib.use("Agg" if args.save else "QtAgg")
    import matplotlib.pyplot as plt
    fig, (axL, axX, axC) = plt.subplots(3, 1, figsize=(13, 11), sharex=True)

    def shade(ax):
        for j, (s, e) in enumerate(bands):
            ax.axvspan(np.datetime64(int(s), "s"), np.datetime64(int(e), "s"),
                       color="orange", alpha=0.13, label="pyUSD being incentivised" if j == 0 else None)

    axL.plot(tt, S["L"] / 1e6, lw=1.6, color="black", label="measured others TVL")
    axL.plot(tt, L / 1e6, lw=1.4, color="crimson", ls="--", label="model (others' own incentive only)")
    shade(axL)
    axL.set_ylabel("others staked TVL [$M]"); axL.grid(alpha=0.3)
    axL.legend(loc="upper right", fontsize=8)
    axL.set_title(f"Non-pyUSD crvUSD pools — dead-band fit, incentive-only "
                  f"(τin {tin:.0f}d / τout {tout:.0f}d, band [{xlo:.2f},{xhi:.2f}]×, "
                  f"rush p_in {P_IN:.2f} from pyUSD, R² {r2:.3f})")

    axX.plot(tt, a / S["m"], lw=1.0, color="steelblue", label="others APR / market (x)")
    axX.axhline(xhi, color="green", ls="--", lw=0.9, label=f"x_hi {xhi:.2f}× (inflow edge)")
    axX.axhline(xlo, color="red", ls="--", lw=0.9, label=f"x_lo {xlo:.2f}× (outflow edge)")
    shade(axX)
    axX.set_ylabel("APR / market rate"); axX.grid(alpha=0.3); axX.legend(loc="upper right", fontsize=8)

    axC.plot(tt, S["others_apr"] * 100, lw=1.2, color="steelblue", label="others APR (CRV)")
    axC.plot(tt, S["py_apr"] * 100, lw=1.2, color="crimson", label="pyUSD APR (CRV+YB)")
    shade(axC)
    axC.set_ylabel("reward APR [%]"); axC.grid(alpha=0.3); axC.legend(loc="upper right", fontsize=8)
    axC.set_title("Competition context: pyUSD vs others reward APR (shaded = pyUSD incentivised)")
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=120); print(f"saved {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
