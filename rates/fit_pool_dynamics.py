#!/usr/bin/env python3
"""Unified TVL-dynamics fit for the pyUSD/crvUSD pool (one ODE vs the whole series).

Instead of per-campaign exponential fits, integrate a single dead-band relaxation
model against the *measured* reward series and fit it to the staked-TVL curve.

Why this is the right tool here: the LP APR is **endogenous** — it is
reward_rate / TVL, so as TVL grows the APR self-limits. Capital flows in until the
APR is driven down to the top of a hysteresis band (~2× the market rate); when a YB
campaign ends the reward drops, APR falls below the bottom edge (~1×), and TVL
decays — but if the campaign refills before the outflow completes, APR jumps back
and the outflow simply stops. All of that emerges from one ODE driven by the real
rates; isolated exponential fits cannot represent the start/stop-on-refill behaviour.

Model (state L = staked TVL, $):
    rewards(t,L) = CRV_value(t) + YB_value(t)                  [$/yr]
    APR a(t,L)   = fee_apr(t) + rewards / L
    x            = a / m(t)                                    m = sUSDS market rate
    dead band [x_lo, x_hi]:
        x > x_hi : inflow   toward L*_in  = rewards/(x_hi·m − fee),  rate 1/τ_in
        x < x_lo : outflow  toward L*_out = rewards/(x_lo·m − fee),  rate 1/τ_out
        else     : hold (LPs inert inside the band)

No boost term. CRV emissions to the gauge are a fixed total split among stakers by
veCRV-boosted weight — boost only redistributes that fixed pot (one staker's gain is
another's loss), so the *average* CRV APR is exactly CRV_value / TVL regardless of
boost. Multiplying by a boost would double-count. (YB is flat, also unboosted.)
Fitted parameters: τ_in, τ_out, x_lo, x_hi.

Usage
-----
    uv run python fit_pool_dynamics.py
    uv run python fit_pool_dynamics.py --save pics/pool_dynamics_fit.png
"""
import argparse
import datetime as dt
import lzma
from pathlib import Path

import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
APR_CSV = HERE / "pool_apr.csv.xz"
SUSDS_CSV = HERE / "susds_rates.csv.xz"
YEAR = 365.0 * 86400.0

# Votemarket Liquidity-Mining YB distributed to pyUSD LPs via StakeDAO/Votemarket
# (the campaign `leftover` swept to the IncentiveGaugeHook → bridged pYB → RewardVault
# for LPs; there were never Merkl campaigns). NOT in the gauge `reward_data(YB)`, so
# the model misses it unless added here. Exact YB per epoch from the StakeDAO/Votemarket
# campaign data (campaign 1435). NOTE: the campaign dates are the *voting* windows;
# LP rewards are distributed **one week later**, so these periods are voting + 1 week.
VOTEMARKET_LM_YB = [
    ("2026-04-16", "2026-04-23", 331_533.0),   # voting Apr 9–16
    ("2026-04-23", "2026-04-30", 331_874.0),   # voting Apr 16–23
]


def _ts(s):
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC).timestamp())


# ~3-day zoom windows around the tiny-pool take-offs (steepest rush-ins).
RUSH_WINDOWS = [
    ("2025-10-16", "2025-10-19", "Oct 2025 rush-in"),
    ("2026-01-31", "2026-02-03", "Feb 2026 rush-in"),
]


def trailing_apr(ts, x, win_days=14.0):
    """Annualised trailing growth of x (fraction, not %)."""
    w = win_days * 86400
    out = np.zeros(len(ts))
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > w:
            j += 1
        if i > j and ts[i] > ts[j] and x[j] > 0:
            out[i] = (x[i] / x[j] - 1.0) / (ts[i] - ts[j]) * YEAR
    return np.clip(out, 0.0, None)


def load_series():
    with lzma.open(APR_CSV) as fh:
        d = pl.read_csv(fh.read())
    g = lambda c: d[c].cast(pl.Float64, strict=False).to_numpy()
    t = g("timestamp")
    vp = g("virtual_price")
    L = g("gauge_staked") * vp                          # staked TVL ($)
    fee = trailing_apr(t, vp)                           # fee APR (fraction)
    crv_val = g("crv_rate") * g("crv_rel_weight") * g("crv_price") * YEAR   # $/yr
    yb_on = t < g("yb_period_finish")
    yb_val = np.where(yb_on, g("yb_rate") * g("yb_price") * YEAR, 0.0)      # $/yr
    # Votemarket LM YB to LPs (annualised $/yr over each weekly epoch, contemporaneous price)
    ybpx = g("yb_price")
    yb_lm_val = np.zeros(t.size)
    lm_on = np.zeros(t.size, bool)
    for s0, e0, amt in VOTEMARKET_LM_YB:
        a, b = _ts(s0), _ts(e0)
        seg = (t >= a) & (t < b)
        yb_lm_val[seg] = amt * ybpx[seg] * YEAR / (b - a)
        lm_on |= seg
    with lzma.open(SUSDS_CSV) as fh:
        s = pl.read_csv(fh.read())
    st, sa = s["timestamp"].to_numpy(), s["susds_apr"].to_numpy()
    m = np.interp(t, st, sa)                            # market rate (fraction)
    return dict(t=t, L=L, fee=fee, crv_val=crv_val, yb_val=yb_val, yb_lm_val=yb_lm_val,
                m=m, yb_on=yb_on | lm_on)


def simulate(S, tau_in_d, tau_out_d, x_lo, x_hi, p_in=0.0):
    t, fee, crv_val, yb_val, m = S["t"], S["fee"], S["crv_val"], S["yb_val"], S["m"]
    yb_lm_val = S["yb_lm_val"]
    tau_in, tau_out = tau_in_d / 365.0, tau_out_d / 365.0
    n = t.size
    L = np.empty(n)
    L[0] = S["L"][0]
    a = np.empty(n)
    for k in range(1, n):
        Lk = L[k - 1]
        rewards = crv_val[k] + yb_val[k] + yb_lm_val[k]   # CRV/TVL already averages out boost
        apr = fee[k] + rewards / Lk
        a[k] = apr
        x = apr / m[k]
        dt = (t[k] - t[k - 1]) / YEAR
        if x > x_hi:
            denom = x_hi * m[k] - fee[k]
            Lt = rewards / denom if denom > 1e-6 else Lk * 4
            # rush: inflow accelerates the further APR is above threshold (tiny-pool
            # appeal). p_in=0 -> plain exponential. Cap the step at the full gap.
            frac = (dt / tau_in) * (x / x_hi) ** p_in
            Lk = Lk + (Lt - Lk) * min(frac, 1.0)
        elif x < x_lo:
            denom = x_lo * m[k] - fee[k]
            Lt = rewards / denom if denom > 1e-6 else Lk * 0.25
            Lk = Lk + (Lt - Lk) * (dt / tau_out)
        # else: hold
        L[k] = max(Lk, 1e3)
    a[0] = fee[0] + (crv_val[0] + yb_val[0] + yb_lm_val[0]) / L[0]
    return L, a


def loss(S, params):
    L, _ = simulate(S, *params)
    e = np.log(L) - np.log(S["L"])
    return float(np.mean(e ** 2))


def fit(S, seed=0, band=None):
    """Fit [tau_in, tau_out, x_lo, x_hi, p_in]. band=(x_lo,x_hi) fixes the
    dead-band. p_in is the inflow-acceleration exponent (0 = plain exponential)."""
    from scipy.optimize import differential_evolution
    full = {"tin": (1.0, 30.0), "tout": (1.0, 30.0), "xlo": (0.7, 1.5),
            "xhi": (1.5, 3.0), "p": (0.0, 4.0)}
    free = ["tin", "tout"] + ([] if band else ["xlo", "xhi"]) + ["p"]
    bounds = [full[k] for k in free]

    def build(pp):
        v = {"tin": None, "tout": None, "xlo": band[0] if band else None,
             "xhi": band[1] if band else None, "p": 0.0}
        for k, val in zip(free, pp):
            v[k] = val
        return [v["tin"], v["tout"], v["xlo"], v["xhi"], v["p"]]

    res = differential_evolution(lambda pp: loss(S, build(pp)), bounds, seed=seed,
                                 maxiter=60, tol=1e-5, polish=True, updating="deferred")
    return np.array(build(res.x)), res.fun


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--x-lo", type=float, default=None, help="fix outflow band edge")
    ap.add_argument("--x-hi", type=float, default=None, help="fix inflow band edge")
    ap.add_argument("--linear", action="store_true",
                    help="linear TVL y-axis (default: semilogy, matching the log-space fit)")
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()

    S = load_series()
    band = (args.x_lo, args.x_hi) if (args.x_lo and args.x_hi) else None
    params, J = fit(S, band=band)
    tin, tout, xlo, xhi, p_in = params
    L, a = simulate(S, *params)
    # R^2 in log space
    e = np.log(L) - np.log(S["L"])
    r2 = 1.0 - np.sum(e ** 2) / np.sum((np.log(S["L"]) - np.log(S["L"]).mean()) ** 2)
    print(f"fit (log-RMSE {np.sqrt(J):.3f}, R^2 {r2:.3f}):")
    print(f"  tau_in        = {tin:.1f} d")
    print(f"  tau_out       = {tout:.1f} d")
    print(f"  p_in (rush)   = {p_in:.2f}   (0 = plain exponential)")
    print(f"  dead band     = [{xlo:.2f}×, {xhi:.2f}×] market"
          + ("  (fixed)" if band else ""))

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
    if args.linear:
        axL.fill_between(tt, 0, 70, where=S["yb_on"], color="orange", alpha=0.10,
                         label="YB campaign on")
        axL.set_ylabel("staked TVL [$M]")
    else:
        axL.set_yscale("log"); axL.set_ylim(0.5, 100)
        axL.fill_between(tt, 0.5, 100, where=S["yb_on"], color="orange", alpha=0.10,
                         label="YB campaign on")
        axL.set_ylabel("staked TVL [$M, log]")
    axL.set_title(f"pyUSD/crvUSD TVL dynamics fit — τin {tin:.0f}d / τout {tout:.0f}d, "
                  f"p_in {p_in:.2f}, band [{xlo:.2f}, {xhi:.2f}]×, R² {r2:.3f}")
    axL.legend(loc="upper left", fontsize=8); axL.grid(alpha=0.3)

    axA.plot(tt, a / S["m"], lw=1.0, color="steelblue", label="APR / market (x)")
    axA.axhline(xhi, color="green", ls="--", lw=0.9, label=f"x_hi {xhi:.2f}× (inflow edge)")
    axA.axhline(xlo, color="red", ls="--", lw=0.9, label=f"x_lo {xlo:.2f}× (outflow edge)")
    axA.set_ylabel("APR / market rate"); axA.set_yscale("log")
    axA.legend(loc="upper right", fontsize=8); axA.grid(alpha=0.3)

    # zoom panels on the tiny-pool rush-ins (semilogy)
    for key, (s0, e0, lab) in zip(zkeys, RUSH_WINDOWS):
        az = axd[key]
        lo, hi = np.datetime64(s0), np.datetime64(e0)
        seg = (tt >= lo) & (tt < hi)
        az.plot(tt[seg], S["L"][seg] / 1e6, lw=1.6, color="black", label="measured")
        az.plot(tt[seg], L[seg] / 1e6, lw=1.6, color="crimson", ls="--", label="model")
        az.set_yscale("log"); az.set_xlim(lo, hi)
        az.set_title(lab, fontsize=9); az.set_ylabel("TVL [$M, log]")
        az.grid(alpha=0.3, which="both"); az.legend(fontsize=7, loc="upper left")
        for t in az.get_xticklabels():
            t.set_rotation(20); t.set_fontsize(7)
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=120); print(f"saved {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
