#!/usr/bin/env python3
"""Is d_filter_time=6h the right derivative smoothing for the net-pressure PID?

The PID (contracts/net_pressure/PID.vy) low-passes the derivative of `pressure` with a
first-order Astrom filter (discrete, dt & Tf in YEARS, exactly as the contract):

    d[k] = (Tf*d[k-1] + (P[k]-P[k-1])) / (Tf + dt)          # Tf = d_filter_time

and feeds only its RISING part:  target += kd * max(0, d).  Real deposits/withdrawals
and swaps land as discrete steps, so the raw derivative (Tf=0) is a spike train.  This
script drives that exact filter with the REAL, per-event net-pressure of the PYUSD/crvUSD
sink pool (fetch_pool_netpressure.py), sampled at the controller's trigger cadence
(min_interval, default 1h), and sweeps Tf to size the noise/responsiveness trade-off.

Metrics per Tf (D-term isolated):
  * RMS / peak of the rising derivative max(0,d) and of its APR footprint
        d_apr = kd*max(0,d)/sink_per_offer * market_rate      (offer -> bonus_apr path)
  * tick-to-tick jitter of d_apr (how much the offered APR flickers from D alone)
  * saturation: fraction of steps where kd*max(0,d) alone >= sink_cap
  * lag vs a slow reference trend (responsiveness cost of smoothing)

Usage:  uv run python sim_pid_dfilter.py [--interval 3600] [--save pics/pid_dfilter.png]
"""
import argparse
import csv
import lzma

import numpy as np

HERE = __file__.rsplit("/", 1)[0]
SPY = 365 * 86400

# --- PID.vy default gains (1e18 -> float) --------------------------------------
KD = 0.0158
SINK_PER_OFFER = 0.5
SINK_CAP = 22.0
DEAD_BAND = 1.6
D_FILTER_TIME = 6 * 3600
MIN_INTERVAL = 3600

TF_GRID_H = [0, 0.5, 1, 2, 3, 6, 12, 24, 48]   # hours; 0 = raw unfiltered derivative


def load_netpressure(path=f"{HERE}/pool_netpressure.csv.xz"):
    rows = list(csv.reader(lzma.open(path, "rt")))
    h = rows[0]; ci = {k: i for i, k in enumerate(h)}
    r = rows[1:]
    t = np.array([int(x[ci["timestamp"]]) for x in r], dtype=np.int64)
    npr = np.array([float(x[ci["net_pressure"]]) for x in r])
    tvl = np.array([float(x[ci["tvl_usd"]]) for x in r])
    return t, npr, tvl


def load_market_rate(t_grid, path=f"{HERE}/aave_rate_smoothed.csv.xz"):
    rows = list(csv.reader(lzma.open(path, "rt")))
    h = rows[0]; ci = {k: i for i, k in enumerate(h)}
    r = rows[1:]
    tm = np.array([int(x[ci["timestamp"]]) for x in r], dtype=np.int64)
    rate = np.array([float(x[ci["aave_apr_ema7d"]]) for x in r])
    return np.interp(t_grid, tm, rate)


def astrom(P, t, Tf):
    """Exact PID.vy discrete derivative filter (dt, Tf in years). Tf=0 -> raw dP/dt."""
    tf = Tf / SPY
    d = np.zeros_like(P)
    for k in range(1, len(P)):
        dt = (t[k] - t[k - 1]) / SPY
        dp = P[k] - P[k - 1]
        d[k] = (tf * d[k - 1] + dp) / (tf + dt)
    return d


def sample_on_grid(t_ev, y_ev, grid):
    """Piecewise-constant sample: value of the last event at/before each grid time."""
    idx = np.searchsorted(t_ev, grid, side="right") - 1
    idx = np.clip(idx, 0, len(y_ev) - 1)
    return y_ev[idx]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=int, default=MIN_INTERVAL,
                    help="controller trigger cadence in seconds (default min_interval=3600)")
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()

    t_ev, npr_ev, tvl_ev = load_netpressure()
    grid = np.arange(t_ev[0], t_ev[-1], args.interval)
    P = sample_on_grid(t_ev, npr_ev, grid)
    mkt = load_market_rate(grid)
    dt_h = args.interval / 3600

    print(f"signal: {len(t_ev):,} events over {(t_ev[-1]-t_ev[0])/86400:.0f} d; "
          f"controller grid {len(grid):,} pts @ {dt_h:.1f}h")
    print(f"net_pressure P: range [{P.min():+.3f}, {P.max():+.3f}]  std {P.std():.3f}")
    dP = np.diff(P)
    print(f"|ΔP| per {dt_h:.0f}h step: p50 {np.median(np.abs(dP)):.5f}  "
          f"p95 {np.percentile(np.abs(dP),95):.4f}  p99 {np.percentile(np.abs(dP),99):.4f}  "
          f"max {np.abs(dP).max():.4f}")
    print(f"market_rate: {mkt.mean()*100:.2f}% avg\n")

    # --- reference "slow trend" derivative to measure responsiveness/lag against.
    # Use a heavily smoothed derivative (Tf = 3 d) as the genuine multi-day trend.
    d_ref = astrom(P, grid, 3 * 86400)

    print(f"{'Tf':>6} | {'d RMS':>8} {'d peak':>8} | {'d_apr RMS':>9} {'d_apr p99':>9} "
          f"{'d_apr max':>9} | {'jitter':>7} | {'sat%':>6} | {'corr':>5} {'lag_h':>5}")
    print(f"{'':>6} | {'[/yr]':>8} {'[/yr]':>8} | {'[%APR]':>9} {'[%APR]':>9} {'[%APR]':>9} "
          f"| {'[%APR]':>7} | {'':>6} | {'ref':>5} {'':>5}")
    print("-" * 104)
    rows = []
    for Tf_h in TF_GRID_H:
        d = astrom(P, grid, Tf_h * 3600)
        dpos = np.maximum(0.0, d)
        d_apr = KD * dpos / SINK_PER_OFFER * mkt * 100      # % APR added by D alone
        jit = np.std(np.diff(d_apr))
        sat = np.mean(KD * dpos >= SINK_CAP) * 100
        # responsiveness: corr with the slow-trend reference, and lag of peak x-corr
        dm = d - d.mean(); rm = d_ref - d_ref.mean()
        corr = float(np.dot(dm, rm) / (np.linalg.norm(dm) * np.linalg.norm(rm) + 1e-30))
        lags = np.arange(-48, 49)
        xc = [np.dot(np.roll(dm, L), rm) for L in lags]
        lag_h = lags[int(np.argmax(xc))] * dt_h
        rows.append(dict(Tf_h=Tf_h, d=d, dpos=dpos, d_apr=d_apr,
                         dRMS=np.sqrt(np.mean(d**2)), dpk=dpos.max(),
                         aprRMS=np.sqrt(np.mean(d_apr**2)), apr99=np.percentile(d_apr, 99),
                         aprmax=d_apr.max(), jit=jit, sat=sat, corr=corr, lag_h=lag_h))
        tag = "  (raw)" if Tf_h == 0 else ("  <-- default" if Tf_h == 6 else "")
        print(f"{Tf_h:>5.1f}h | {rows[-1]['dRMS']:>8.1f} {rows[-1]['dpk']:>8.0f} | "
              f"{rows[-1]['aprRMS']:>9.2f} {rows[-1]['apr99']:>9.2f} {rows[-1]['aprmax']:>9.1f} | "
              f"{jit:>7.3f} | {sat:>5.1f}% | {corr:>5.2f} {lag_h:>4.0f}h{tag}")

    # --- variance signature: RMS derivative vs averaging horizon (noise structure)
    def signature(series, dth):
        out = []
        for hh in [1, 2, 3, 6, 12, 24, 48, 96, 168]:
            n = int(round(hh / dth))
            if 1 <= n < len(series):
                d = series[n:] - series[:-n]
                out.append((hh, d.std(), d.std() / (n * dth * 3600 / SPY)))
        return out

    sig = signature(P, dt_h)
    print("\nvariance signature  std(P[t+h]-P[t]) and implied RMS derivative:")
    print(f"{'h':>6} {'std ΔP':>8} {'RMS d/dt[/yr]':>14}")
    for hh, s, rmsd in sig:
        print(f"{hh:>5}h {s:>8.4f} {rmsd:>14.1f}")

    # --- cross-check vs the ACTUAL YB BTC net pressure (real-price-driven sim dump):
    # the sink pool is a conservative stress test; rescale D-term numbers by the ratio.
    yb_sig, ratio = yb_reference(dt_h, P.std())
    if ratio:
        print(f"\nYB-BTC net pressure (dump) std vs pyUSD sink std: ratio {ratio:.1f}x "
              f"(sink is {ratio:.1f}x noisier).")
        print(f"  => on the real YB signal, expect the D-term APR figures above to be "
              f"~{ratio:.1f}x SMALLER.")
        for Tf_h in (0, 6):
            r = next(x for x in rows if x["Tf_h"] == Tf_h)
            tag = "raw" if Tf_h == 0 else f"{Tf_h}h"
            print(f"     Tf={tag:>3}: sink D-APR RMS {r['aprRMS']:.2f}% -> YB ~{r['aprRMS']/ratio:.2f}%; "
                  f"p99 {r['apr99']:.1f}% -> YB ~{r['apr99']/ratio:.1f}%")

    if args.save:
        make_figure(grid, P, t_ev, npr_ev, tvl_ev, rows, sig, yb_sig, dt_h, args.save)


def yb_reference(dt_h, sink_std):
    """RMS-derivative signature + std of the real YB BTC net pressure (one sim dump).
    Returns (signature_list, sink/YB std ratio) or ([], None) if the dump is absent."""
    import glob
    try:
        import net_pressure as N
        p = sorted(glob.glob(f"{HERE}/btc-candidates-yb-opt/**/detailed-output.npz.xz",
                             recursive=True))
        if not p:
            return [], None
        d = N.load_npz_xz(p[0])
        t = d["t"].astype(np.int64)
        t0 = d["token0"].astype(float); t1 = d["token1"].astype(float)
        debt = (t0 + t1 * d["price_scale"].astype(float)) / 2.0
        tvl = t0 + t1 * d["p_cex"].astype(float)
        npr = 2.0 * (debt - t0) / tvl
        o = np.argsort(t); t, npr = t[o], npr[o]
        grid = np.arange(t[0], t[-1], int(dt_h * 3600))
        P = npr[np.clip(np.searchsorted(t, grid, side="right") - 1, 0, len(npr) - 1)]
        sig = []
        for hh in [1, 2, 3, 6, 12, 24, 48, 96, 168]:
            n = int(round(hh / dt_h))
            if 1 <= n < len(P):
                dd = P[n:] - P[:-n]
                sig.append((hh, dd.std(), dd.std() / (n * dt_h * 3600 / SPY)))
        return sig, sink_std / P.std()
    except Exception:
        return [], None


def make_figure(grid, P, t_ev, npr_ev, tvl_ev, rows, sig, yb_sig, dt_h, save):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    gd = grid.astype("datetime64[s]")
    ted = t_ev.astype("datetime64[s]")
    byTf = {r["Tf_h"]: r for r in rows}

    fig, ax = plt.subplot_mosaic(
        [["sig", "sig"], ["zoom", "draw"], ["curve", "vsig"]],
        figsize=(15, 13), height_ratios=[2.4, 3, 3])

    # (a) full net_pressure + TVL
    a = ax["sig"]
    a.plot(gd, P, lw=0.5, color="#2b6cb0")
    a.set_ylabel("net_pressure  2·(PYUSD−crvUSD)/TVL", color="#2b6cb0")
    a.set_title("PYUSD/crvUSD sink-pool net pressure — real per-event signal (2025-10 … 2026-07)")
    a.grid(alpha=0.3)
    at = a.twinx()
    at.plot(ted, tvl_ev / 1e6, lw=0.8, color="#dd6b20", alpha=0.7)
    at.set_ylabel("pool TVL [$M]", color="#dd6b20"); at.set_yscale("log")

    # pick a busy 12-day window for zoom panels (max hourly |ΔP|)
    dP = np.abs(np.diff(P))
    c = int(np.argmax(dP))
    w = int(round(6 * 86400 / (dt_h * 3600)))
    lo, hi = max(0, c - w), min(len(grid), c + w)
    tlo, thi = grid[lo], grid[hi - 1]
    segev = (t_ev >= tlo) & (t_ev <= thi)

    # (b) zoom: per-event steps vs hourly controller samples
    b = ax["zoom"]
    b.step(ted[segev], npr_ev[segev], where="post", lw=0.7, color="#718096",
           label="per-event (on-chain)")
    b.plot(gd[lo:hi], P[lo:hi], ".", ms=3, color="#2b6cb0",
           label=f"controller samples @ {dt_h:.0f}h")
    b.set_ylabel("net_pressure"); b.set_title("Zoom: stepwise on-chain signal vs controller sampling")
    b.legend(fontsize=8, loc="best"); b.grid(alpha=0.3)

    # (c) rising D-term APR footprint for a few Tf over the zoom
    d = ax["draw"]
    for Tf_h, col in [(0, "#e53e3e"), (1, "#dd6b20"), (6, "#2f855a"), (24, "#553c9a")]:
        r = byTf[Tf_h]
        lab = "raw (Tf=0)" if Tf_h == 0 else f"Tf={Tf_h:g}h" + (" (default)" if Tf_h == 6 else "")
        d.plot(gd[lo:hi], r["d_apr"][lo:hi], lw=1.1 if Tf_h else 0.8, color=col, label=lab,
               alpha=0.9)
    d.set_ylabel("D-term APR add [%]"); d.set_yscale("symlog", linthresh=0.5)
    d.set_title("Bonus-APR contributed by the D-term alone: kd·max(0,d)/β·market_rate")
    d.legend(fontsize=8, loc="upper right"); d.grid(alpha=0.3)
    d.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))

    # (e) trade-off curves vs Tf
    e = ax["curve"]
    tf = np.array([r["Tf_h"] for r in rows[1:]])   # skip raw for log axis
    e.plot(tf, [r["aprRMS"] for r in rows[1:]], "o-", color="#2b6cb0", label="D-term APR RMS [%]")
    e.plot(tf, [r["apr99"] for r in rows[1:]], "s--", color="#dd6b20", label="D-term APR p99 [%]")
    e.plot(tf, [r["jit"] for r in rows[1:]], "^-", color="#e53e3e", label="tick jitter [%APR]")
    e.axvline(6, color="k", ls=":", lw=1); e.text(6, e.get_ylim()[1], " 6h default", fontsize=8, va="top")
    e.set_xscale("log"); e.set_xlabel("d_filter_time Tf [h]"); e.set_ylabel("[% APR]")
    e.set_title("D-term noise vs Tf (lower = calmer offered APR)")
    e.set_xticks(tf); e.set_xticklabels([f"{x:g}" for x in tf])
    e.legend(fontsize=8, loc="upper right"); e.grid(alpha=0.3, which="both")
    ec = e.twinx()   # responsiveness COST: step-response half-life ~ Tf*ln2
    ec.plot(tf, tf * np.log(2), "d-", color="#2f855a", alpha=0.6)
    ec.set_ylabel("D step-response half-life [h]  (lag cost)", color="#2f855a")

    # (f) variance signature — sink pool vs the real (calmer) YB BTC net pressure
    f = ax["vsig"]
    hh = np.array([s[0] for s in sig]); rmsd = np.array([s[2] for s in sig])
    f.plot(hh, rmsd, "o-", color="#553c9a", label="PYUSD sink pool (this study)")
    if yb_sig:
        yh = np.array([s[0] for s in yb_sig]); yr = np.array([s[2] for s in yb_sig])
        f.plot(yh, yr, "s--", color="#2b6cb0", label="YB BTC net pressure (sim dump)")
    f.axvline(6, color="k", ls=":", lw=1); f.text(6, rmsd.max(), " 6h", fontsize=8, va="top")
    f.set_xscale("log"); f.set_yscale("log")
    f.set_xlabel("averaging horizon h [h]"); f.set_ylabel("RMS derivative [/yr]")
    f.set_title("Derivative-noise vs horizon: smooth (no knee) → Tf is a noise/lag trade-off")
    f.set_xticks(hh); f.set_xticklabels([f"{x:g}" for x in hh])
    f.legend(fontsize=8); f.grid(alpha=0.3, which="both")

    for k in ("sig", "zoom", "draw"):
        for l in ax[k].get_xticklabels():
            l.set_fontsize(8)
    fig.tight_layout()
    fig.savefig(save, dpi=120)
    print(f"\nsaved {save}")


if __name__ == "__main__":
    main()
