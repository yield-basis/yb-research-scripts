#!/usr/bin/env python3
"""Testbed for crvUSD supply-sink incentive models, optimised against net pressure.

Idea
----
When LevAMM runs a positive net pressure (crvUSD shortfall in the sink), we pay a
temporary bonus APR on scrvUSD. That raises its APR; crvUSD depositors arrive
(slowly) and the incremental sink S absorbs the shortfall. We want S to cover the
positive pressure P while spending as little as possible — and not much more than
needed.

Everything is in **normalised units**: P and S are fractions of half-TVL, i.e. the
same units as `net_pressure` (so results are scale-free; multiply by half-TVL for $).

Measured anchors (see REPORT_pool_apr_response.md / REPORT_liquidity_response.md)
  * inflow time constant  tau_in  ~ 9 d   (deposits arrive slowly)
  * outflow time constant tau_out ~ 4.5 d (leave faster)
  * hysteresis / dead-band: crvUSD depositors don't move until scrvUSD APR >= 2x
    the market norm; base scrvUSD APR ~ 1x market.
  * market norm = Aave USDC APR (only series covering 2024-2026, incl. the
    2024-08-05 stress event).

Model
-----
  signal      P(t) = max(0, net_pressure)                       [frac of half-TVL]
  controller  S*(t) = f(P, S, state)        (the sink we aim to attract)
  offer       x(t) = 2 + S*/beta            (advertised APR as a multiple of market;
              must clear the 2x dead-band, plus more to pull volume; beta = sink
              attracted per unit of excess ratio — the key calibration unknown)
  dynamics    dS/dt = (S* - S)/tau,  tau = tau_in if S*>S else tau_out
  spend rate  (x-1)*m*S      (bonus APR above base 1x market, paid ONLY on the
              attracted sink S — per design choice)

Cost J = spend + lambda * undercoverage_area.  Overshoot is self-penalising (a
bigger S* costs more spend), so only undercoverage needs an explicit weight.

Usage
-----
    uv run python incentive_sim.py --controller pi --optimize
    uv run python incentive_sim.py --controller pi --optimize --save pics/incentive_pi.png
"""
import argparse
import lzma
from pathlib import Path

import numpy as np

from net_pressure import load_npz_xz, PRICE_KEY

HERE = Path(__file__).resolve().parent
ROOT = "btc-candidates-yb-opt"
WORST = (f"{ROOT}/btc_a5_mf120_of163_fg00850937_don0187374_rpf433333/"
         "detailed-output.npz.xz")

YEAR = 365.25 * 86400.0
TAU_IN = 9.0 / 365.25     # years
TAU_OUT = 4.5 / 365.25    # years
HYST = 2.0                # dead-band: need 2x market APR to attract crvUSD
BETA = 0.5                # sink (frac half-TVL) attracted per unit excess ratio
SCAP = 3.0                # cap on target sink
BUFFER = 0.20             # net pressure already absorbed by the YB-funded standing
                          # buffer; the scrvUSD scheme only covers the residual above it

# ----------------------------------------------------------------------------- signal


def net_pressure_series(path):
    d = load_npz_xz(path)
    t = d["t"].astype(np.int64)
    token0 = d["token0"].astype(np.float64)
    token1 = d["token1"].astype(np.float64)
    ps = d["price_scale"].astype(np.float64)
    price = d[PRICE_KEY].astype(np.float64)
    debt = (token0 + token1 * ps) / 2.0
    tvl = token0 + token1 * price
    with np.errstate(divide="ignore", invalid="ignore"):
        npr = 2.0 * (debt - token0) / tvl
    ok = np.isfinite(npr) & (tvl > 0)
    t, npr = t[ok], npr[ok]
    order = np.argsort(t)
    return t[order], npr[order]


def load_market(path=str(HERE / "aave_usdc_rates.csv.xz")):
    """Aave USDC APR as (timestamps, apr_fraction)."""
    import polars as pl
    with lzma.open(path) as fh:
        df = pl.read_csv(fh.read())
    t = df["timestamp"].to_numpy().astype(np.int64)
    apr = df["aave_usdc_apr"].to_numpy().astype(np.float64)
    order = np.argsort(t)
    return t[order], apr[order]


def ema(series, dt_year, tau_year):
    """Causal exponential moving average with time constant tau_year."""
    if tau_year <= 0:
        return series.copy()
    a = dt_year / tau_year
    a = min(a, 1.0)
    out = np.empty_like(series)
    acc = series[0]
    for k in range(series.size):
        acc += (series[k] - acc) * a
        out[k] = acc
    return out


def build_grid(path, dt_hours=4.0, smooth_days=7.0, buffer=BUFFER):
    """Resample pressure and market rate onto a uniform grid.

    The Aave norm is spiky (unlike sUSDS), so it is EMA-smoothed with a
    `smooth_days` time constant — depositors react to a trailing average rate,
    not intraday spikes. Returns the smoothed norm `m` plus the raw `m_raw`.

    `buffer` is the net pressure already handled by the YB-funded standing buffer;
    the scrvUSD scheme only sees the residual `P = max(0, net_pressure - buffer)`.
    """
    t, npr = net_pressure_series(path)
    mt, mapr = load_market()
    step = int(dt_hours * 3600)
    grid = np.arange(t[0], t[-1] + step, step, dtype=np.int64)
    P = np.clip(np.interp(grid, t, npr) - buffer, 0.0, None)   # residual above buffer
    m_raw = np.interp(grid, mt, mapr, left=mapr[0], right=mapr[-1])
    dt_year = step / YEAR
    m = ema(m_raw, dt_year, smooth_days / 365.25)             # smoothed market norm
    return grid, P, m, m_raw, dt_year


# ----------------------------------------------------------------------------- controllers
# Each controller is (param_names, bounds, step_fn). step_fn(params, P_k, S, state)
# returns (S_target, new_state).

def _ff_step(p, Pk, S, state):
    (alpha,) = p
    return alpha * Pk, state


def _pi_step(p, Pk, S, state):
    alpha, Kp, Ki, Imax = p
    I = state["I"] + (Pk - S) * state["dt"]
    I = min(max(I, 0.0), Imax)
    state["I"] = I
    return alpha * Pk + Kp * (Pk - S) + Ki * I, state


def _pid_step(p, Pk, S, state):
    """PI + a derivative term on RISING pressure (dP/dt > 0), driven by how fast
    the price is gapping. Pre-empts a developing spike before the integral builds.
    """
    alpha, Kp, Ki, Kd, Imax = p
    I = state["I"] + (Pk - S) * state["dt"]
    I = min(max(I, 0.0), Imax)
    state["I"] = I
    dPdt = (Pk - state.get("prevP", Pk)) / state["dt"]   # per year
    state["prevP"] = Pk
    return alpha * Pk + Kp * (Pk - S) + Ki * I + Kd * max(0.0, dPdt), state


CONTROLLERS = {
    # feed-forward: aim the sink at a multiple of current pressure
    "ff": (["alpha"], [(0.0, 5.0)], _ff_step),
    # proportional-integral on the coverage error, plus feed-forward
    "pi": (["alpha", "Kp", "Ki", "Imax"],
           [(0.0, 3.0), (0.0, 50.0), (0.0, 2000.0), (0.0, 5.0)], _pi_step),
    # PID: add derivative on rising pressure (the "react to price velocity" term)
    "pid": (["alpha", "Kp", "Ki", "Kd", "Imax"],
            [(0.0, 3.0), (0.0, 50.0), (0.0, 2000.0), (0.0, 0.1), (0.0, 5.0)],
            _pid_step),
}


# ----------------------------------------------------------------------------- simulate


def simulate(P, m, dt_year, ctrl_name, params, beta=BETA, scap=SCAP,
             tau_in=TAU_IN, tau_out=TAU_OUT):
    _, _, step_fn = CONTROLLERS[ctrl_name]
    n = P.size
    S = np.zeros(n)
    Star = np.zeros(n)
    x = np.zeros(n)
    iapr = np.zeros(n)
    s = 0.0
    state = {"I": 0.0, "dt": dt_year}
    for k in range(n):
        st, state = step_fn(params, P[k], s, state)
        st = min(max(st, 0.0), scap)
        tau = tau_in if st > s else tau_out
        s = s + (st - s) * (dt_year / tau)
        Star[k] = st
        S[k] = s
        if st > 0.0:
            xk = HYST + st / beta            # offered APR multiple of market
            x[k] = xk
            iapr[k] = max(0.0, xk - 1.0) * m[k]   # bonus APR above base (1x market)
    spend_rate = iapr * S
    return {"S": S, "Star": Star, "x": x, "iapr": iapr, "spend_rate": spend_rate}


def metrics(P, m, dt_year, sim, lam=1.0, eval_reserve=0.0):
    """eval_reserve: a flat standing reserve (e.g. the 20% YB buffer) applied ONLY
    at evaluation, on top of the controller's sink — it does not change the control
    (the scheme is sized for the full pressure). Reports the residual uncovered
    once that reserve also absorbs its share at each step."""
    S = sim["S"]
    spend = float(np.sum(sim["spend_rate"]) * dt_year)             # frac-half-TVL * APR * yr
    deficit = np.clip(P - S, 0.0, None)
    deficit_res = np.clip(deficit - eval_reserve, 0.0, None)       # with reserve on top
    under = float(np.sum(deficit) * dt_year)
    p_area = float(np.sum(P) * dt_year)
    years = P.size * dt_year
    active = sim["Star"] > 0
    return {
        "J": spend + lam * under,
        "spend": spend,
        "under": under,
        "coverage": 1.0 - under / p_area if p_area > 0 else 1.0,
        "peak_deficit": float(deficit.max()),
        # fraction of all time with a meaningful uncovered shortfall (> 1% half-TVL)
        "frac_uncov": float(np.mean(deficit > 0.01)),
        # same, but crediting the eval_reserve as extra capacity on top
        "peak_deficit_res": float(deficit_res.max()),
        "frac_uncov_res": float(np.mean(deficit_res > 0.01)),
        "spend_pa": spend / years,            # annualised, as frac of half-TVL
        "mean_x_active": float(sim["x"][active].mean()) if active.any() else 0.0,
        "peak_x": float(sim["x"].max()),
        "frac_active": float(active.mean()),
        "years": years,
    }


def cost(P, m, dt_year, ctrl_name, params, lam=1.0, **kw):
    sim = simulate(P, m, dt_year, ctrl_name, params, **kw)
    return metrics(P, m, dt_year, sim, lam=lam)["J"]


def optimize(P, m, dt_year, ctrl_name, lam=1.0, seed=0, **kw):
    from scipy.optimize import differential_evolution
    names, bounds, _ = CONTROLLERS[ctrl_name]
    res = differential_evolution(
        lambda pp: cost(P, m, dt_year, ctrl_name, pp, lam=lam, **kw),
        bounds, seed=seed, maxiter=40, tol=1e-4, polish=True, updating="deferred")
    return dict(zip(names, res.x)), res.fun


def sweep_beta(P, m, dt_year, ctrl_name, betas, lam=1.0):
    """Re-optimise the controller at each beta; return per-beta metrics."""
    names, _, _ = CONTROLLERS[ctrl_name]
    rows = []
    for b in betas:
        params, _ = optimize(P, m, dt_year, ctrl_name, lam=lam, beta=b)
        sim = simulate(P, m, dt_year, ctrl_name, [params[k] for k in names], beta=b)
        mt = metrics(P, m, dt_year, sim, lam=lam)
        mt["beta"] = b
        rows.append(mt)
        print(f"  beta={b:5.2f}  coverage={mt['coverage']*100:6.2f}%  "
              f"spend={mt['spend_pa']*100:7.4f}%/yr  "
              f"peak_deficit={mt['peak_deficit']*100:5.2f}%  "
              f"mean_x={mt['mean_x_active']:.2f}")
    return rows


def compare_controllers(grid, P, m, dt_year, ctrls=("pi", "pid"), beta=BETA,
                        lam=1.0, save=None):
    """Optimise several controllers, print metrics, overlay sink on the 2024-08 zoom."""
    import datetime as dt
    import matplotlib
    matplotlib.use("Agg" if save else "QtAgg")
    import matplotlib.pyplot as plt

    sims = {}
    for c in ctrls:
        names, _, _ = CONTROLLERS[c]
        params, _ = optimize(P, m, dt_year, c, lam=lam, beta=beta)
        sim = simulate(P, m, dt_year, c, [params[k] for k in names], beta=beta)
        mt = metrics(P, m, dt_year, sim, lam=lam)
        sims[c] = (params, sim, mt)
        print(f"[{c:3s}] coverage={mt['coverage']*100:.2f}%  "
              f"spend={mt['spend_pa']*100:.4f}%/yr  "
              f"peak_deficit={mt['peak_deficit']*100:.2f}%  "
              f"| params: " + ", ".join(f"{k}={v:.4g}" for k, v in params.items()))

    tt = grid.astype("datetime64[s]")
    lo = np.datetime64("2024-08-01"); hi = np.datetime64("2024-08-15")
    mwin = (tt >= lo) & (tt < hi)
    colors = {"pi": "steelblue", "pid": "seagreen", "ff": "purple"}

    fig, (axA, axB) = plt.subplots(2, 1, figsize=(13, 9))
    # full range
    axA.plot(tt, P * 100, lw=0.7, color="crimson", label="net pressure P")
    for c in ctrls:
        axA.plot(tt, sims[c][1]["S"] * 100, lw=0.9, color=colors.get(c),
                 label=f"S ({c}), cov {sims[c][2]['coverage']*100:.1f}%, "
                       f"spend {sims[c][2]['spend_pa']*100:.3f}%/yr")
    axA.set_ylabel("frac of half-TVL [%]")
    axA.set_title(f"PI vs PID — full range (beta={beta})")
    axA.legend(loc="upper right", fontsize=8); axA.grid(alpha=0.3)
    # 2024-08 zoom
    axB.plot(tt[mwin], P[mwin] * 100, lw=1.0, color="crimson", label="net pressure P")
    for c in ctrls:
        axB.plot(tt[mwin], sims[c][1]["S"][mwin] * 100, lw=1.3, color=colors.get(c),
                 label=f"S ({c}), peak deficit {sims[c][2]['peak_deficit']*100:.1f}%")
    axB.set_ylabel("frac of half-TVL [%]"); axB.set_xlim(lo, hi)
    axB.set_title("Zoom: 2024-08 crash — does the D term catch the spike?")
    axB.legend(loc="upper right", fontsize=8); axB.grid(alpha=0.3)
    fig.tight_layout()
    if save:
        fig.savefig(save, dpi=120); print(f"saved {save}")
    else:
        plt.show()
    return sims


def sweep_scap(P, m, dt_year, ctrl_name, scaps, beta=BETA, lam=1.0,
               eval_reserves=(0.0, 0.10, 0.20)):
    """Re-optimise while raising the offer cap (SCAP): how high an APR burst is
    needed to fill the sharp spike, and how much peak deficit it buys back.

    Each reserve in `eval_reserves` (e.g. 0.10, 0.20) is credited only at
    evaluation as extra capacity on top of the scheme — the controller still covers
    the full pressure itself, so coverage doesn't depend on the reserve size. For a
    peak deficit `d`, the max uncovered with reserve `r` is max(0, d − r); the time
    uncovered needs the full series, so we compute both here per reserve."""
    names, _, _ = CONTROLLERS[ctrl_name]
    rows = []
    for sc in scaps:
        params, _ = optimize(P, m, dt_year, ctrl_name, lam=lam, beta=beta, scap=sc)
        sim = simulate(P, m, dt_year, ctrl_name, [params[k] for k in names],
                       beta=beta, scap=sc)
        mt = metrics(P, m, dt_year, sim, lam=lam)
        deficit = np.clip(P - sim["S"], 0.0, None)
        mt["res"] = {}   # reserve -> (max_uncovered, frac_time_uncovered)
        for r in eval_reserves:
            dres = np.clip(deficit - r, 0.0, None)
            mt["res"][r] = (float(dres.max()), float(np.mean(dres > 0.01)))
        mt["scap"] = sc
        rows.append(mt)
        cols = "  ".join(f"r{int(r*100):02d}: {mt['res'][r][0]*100:5.2f}%/"
                         f"{mt['res'][r][1]*100:.2f}%t" for r in eval_reserves)
        print(f"  scap={sc:5.1f}  peak_APR={mt['peak_x']:5.1f}x  "
              f"spend={mt['spend_pa']*100:.4f}%/yr  | max_uncov/time @ reserve  {cols}")
    return rows


def plot_scap(rows, eval_reserves=(0.0, 0.10, 0.20), save=None):
    import matplotlib
    matplotlib.use("Agg" if save else "QtAgg")
    import matplotlib.pyplot as plt
    px = [r["peak_x"] for r in rows]
    colors = {0.0: "crimson", 0.10: "darkorange", 0.20: "seagreen"}
    fig, ax = plt.subplots(figsize=(9, 5.5))
    for rv in eval_reserves:
        c = colors.get(rv, "gray")
        lbl = "scheme alone" if rv == 0 else f"+ {rv*100:.0f}% reserve"
        ax.plot(px, [r["res"][rv][0] * 100 for r in rows], "o-", color=c,
                label=f"max uncovered, {lbl} [%]")
    ax.set_xlabel("peak offered APR (multiple of market)")
    ax.set_ylabel("max uncovered net pressure [% half-TVL]")
    ax.set_xscale("log")
    ax.grid(alpha=0.3)
    ax.set_title("High short APR burst fills the spike (scheme covers full pressure, PID)")
    ax.legend(loc="upper right", fontsize=9)
    fig.tight_layout()
    if save:
        fig.savefig(save, dpi=120); print(f"saved {save}")
    else:
        plt.show()


def plot_sweep(rows, ctrl_name, save=None):
    import matplotlib
    matplotlib.use("Agg" if save else "QtAgg")
    import matplotlib.pyplot as plt
    b = [r["beta"] for r in rows]
    fig, ax = plt.subplots(figsize=(9, 5.5))
    ax.plot(b, [r["spend_pa"] * 100 for r in rows], "o-", color="darkorange",
            label="spend [%/yr of half-TVL]")
    ax.set_xlabel("beta  (sink attracted per unit excess APR ratio)")
    ax.set_ylabel("spend [%/yr of half-TVL]", color="darkorange")
    ax.tick_params(axis="y", labelcolor="darkorange")
    ax.set_xscale("log")
    ax.grid(alpha=0.3)
    ax2 = ax.twinx()
    ax2.plot(b, [r["coverage"] * 100 for r in rows], "s-", color="steelblue",
             label="coverage [%]")
    ax2.plot(b, [r["peak_deficit"] * 100 for r in rows], "^--", color="crimson",
             alpha=0.7, label="peak deficit [%]")
    ax2.set_ylabel("coverage / peak deficit [%]", color="steelblue")
    ax2.tick_params(axis="y", labelcolor="steelblue")
    ax.set_title(f"{ctrl_name} controller — spend vs deposit elasticity (beta)")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="center right", fontsize=8)
    fig.tight_layout()
    if save:
        fig.savefig(save, dpi=120)
        print(f"saved {save}")
    else:
        plt.show()


# ----------------------------------------------------------------------------- plot


def plot(grid, P, m, m_raw, sim, mets, ctrl_name, params, save=None):
    import matplotlib
    matplotlib.use("Agg" if save else "QtAgg")
    import matplotlib.pyplot as plt

    tt = grid.astype("datetime64[s]")
    fig, axes = plt.subplots(3, 1, figsize=(13, 10), sharex=True)

    ax = axes[0]
    ax.plot(tt, P * 100, lw=0.8, color="crimson", label="net pressure P")
    ax.plot(tt, sim["S"] * 100, lw=1.0, color="steelblue", label="sink S (absorbed)")
    ax.fill_between(tt, sim["S"] * 100, P * 100, where=P > sim["S"],
                    color="crimson", alpha=0.15, label="uncovered")
    ax.set_ylabel("frac of half-TVL [%]")
    ax.set_title(f"{ctrl_name} controller — coverage {mets['coverage']*100:.1f}%, "
                 f"spend {mets['spend_pa']*100:.3f}%/yr of half-TVL")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)

    ax = axes[1]
    ax.plot(tt, sim["x"], lw=0.8, color="purple", label="offered APR / market (x)")
    ax.axhline(2.0, color="k", ls="--", lw=0.8, label="2x dead-band")
    ax.set_ylabel("APR multiple")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)

    ax = axes[2]
    ax.plot(tt, sim["iapr"] * 100, lw=0.8, color="darkorange", label="incentive APR")
    ax.plot(tt, m_raw * 100, lw=0.5, color="lightgray", label="Aave raw")
    ax.plot(tt, m * 100, lw=1.0, color="gray", label="market norm (Aave, smoothed)")
    ax.set_ylabel("APR [%]")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)

    fig.tight_layout()
    if save:
        fig.savefig(save, dpi=120)
        print(f"saved {save}")
    else:
        plt.show()


# ----------------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidate", default=WORST, help="detailed-output.npz.xz")
    ap.add_argument("--controller", default="pi", choices=list(CONTROLLERS))
    ap.add_argument("--dt-hours", type=float, default=4.0)
    ap.add_argument("--smooth-days", type=float, default=7.0,
                    help="EMA time constant for the Aave norm (default 7 d)")
    ap.add_argument("--buffer", type=float, default=BUFFER,
                    help="net pressure absorbed by the YB-funded buffer (default 0.20)")
    ap.add_argument("--beta", type=float, default=BETA, help="sink per unit excess ratio")
    ap.add_argument("--lam", type=float, default=1.0, help="undercoverage weight")
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--sweep-beta", action="store_true",
                    help="re-optimise across a range of beta and plot spend vs coverage")
    ap.add_argument("--compare-pid", action="store_true",
                    help="optimise PI vs PID and overlay sink on the 2024-08 zoom")
    ap.add_argument("--sweep-scap", action="store_true",
                    help="raise the offer cap and see how a high APR burst fills the spike")
    ap.add_argument("--scap", type=float, default=SCAP, help="offer cap (target sink)")
    ap.add_argument("--eval-reserve", type=float, default=0.0,
                    help="standing reserve credited only at evaluation (extra insurance)")
    ap.add_argument("--params", type=float, nargs="*", help="fixed controller params")
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()

    grid, P, m, m_raw, dt_year = build_grid(args.candidate, dt_hours=args.dt_hours,
                                            smooth_days=args.smooth_days,
                                            buffer=args.buffer)
    print(f"grid: {P.size:,} steps  dt={args.dt_hours}h  "
          f"{str(grid[0].astype('datetime64[s]'))[:10]} .. "
          f"{str(grid[-1].astype('datetime64[s]'))[:10]}")
    print(f"residual pressure (above {args.buffer*100:.0f}% buffer): "
          f"mean {P.mean()*100:.3f}%  peak {P.max()*100:.2f}%  "
          f"active {(P>0).mean()*100:.1f}% of time  beta={args.beta}")

    names, _, _ = CONTROLLERS[args.controller]
    if args.compare_pid:
        compare_controllers(grid, P, m, dt_year, ctrls=("pi", "pid"),
                            beta=args.beta, lam=args.lam, save=args.save)
        return
    if args.sweep_beta:
        betas = [0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.5, 2.0]
        print(f"sweeping beta for [{args.controller}] controller:")
        rows = sweep_beta(P, m, dt_year, args.controller, betas, lam=args.lam)
        plot_sweep(rows, args.controller, save=args.save)
        return
    if args.sweep_scap:
        scaps = [3.0, 5.0, 10.0, 20.0, 40.0, 80.0]
        print(f"sweeping offer cap (scap) for [{args.controller}] controller:")
        rows = sweep_scap(P, m, dt_year, args.controller, scaps,
                          beta=args.beta, lam=args.lam)
        plot_scap(rows, save=args.save)
        return
    if args.optimize:
        params, J = optimize(P, m, dt_year, args.controller, lam=args.lam,
                             beta=args.beta, scap=args.scap)
        print(f"optimised [{args.controller}] J={J:.4g}: "
              + ", ".join(f"{k}={v:.4g}" for k, v in params.items()))
        params = [params[k] for k in names]
    elif args.params:
        params = args.params
    else:
        # sensible default: feed-forward target = 1.2x pressure, mild PI
        defaults = {"ff": [1.2], "pi": [1.2, 5.0, 200.0, 1.0]}
        params = defaults[args.controller]
    print("params:", dict(zip(names, params)))

    sim = simulate(P, m, dt_year, args.controller, params, beta=args.beta, scap=args.scap)
    mets = metrics(P, m, dt_year, sim, lam=args.lam, eval_reserve=args.eval_reserve)
    print(f"coverage      : {mets['coverage']*100:.2f}%")
    print(f"peak deficit  : {mets['peak_deficit']*100:.2f}% of half-TVL")
    print(f"time uncovered: {mets['frac_uncov']*100:.2f}% (deficit > 1% half-TVL)")
    if args.eval_reserve > 0:
        print(f"  w/{args.eval_reserve*100:.0f}% reserve: max uncovered "
              f"{mets['peak_deficit_res']*100:.2f}%, time {mets['frac_uncov_res']*100:.2f}%")
    print(f"spend         : {mets['spend_pa']*100:.4f}%/yr of half-TVL  (total {mets['spend']:.4g})")
    print(f"offer (x)     : mean {mets['mean_x_active']:.2f}x active, peak {mets['peak_x']:.1f}x "
          f"(active {mets['frac_active']*100:.1f}% of time)")

    plot(grid, P, m, m_raw, sim, mets, args.controller, params, save=args.save)


if __name__ == "__main__":
    main()
