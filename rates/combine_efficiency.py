#!/usr/bin/env python3
"""How much peer TVL does the incentivised pyUSD pool cannibalise? (leakage fraction k)

Combines the two calibrated models:
  * pyUSD   (fit_pool_dynamics.py)   — its measured TVL, L_pyUSD(t)
  * others  (fit_others_dynamics.py) — measured TVL L_meas(t) and the own-incentive
                                        dead-band model L_model(t), which has NO
                                        competition term (the (A)-only counterfactual:
                                        what the peers "should" hold on their own
                                        fading incentive).

The peers sit *below* that own-incentive model while pyUSD is incentivised (their
reward stays ~flat yet TVL drains and their APR multiple x ticks up — the signature of
capital leaving for a better-paid home, REPORT_others_dynamics.md). So fit the single
leakage coefficient k that closes the gap:

    L_meas(t) + k · L_pyUSD(t)  ≈  L_model(t)          (least squares, through origin)
    ⇒  k = Σ L_pyUSD·(L_model − L_meas) / Σ L_pyUSD²

k is "what % of the pyUSD pool's TVL must be added back to the peers to match their
own-incentive model" — i.e. the share of pyUSD's TVL that came out of peers. The
incentive's *net* new-liquidity efficiency is then (1 − k).

Usage: uv run python combine_efficiency.py --save pics/incentive_efficiency.png
"""
import argparse

import numpy as np

import fit_others_dynamics as OT
import fit_pool_dynamics as PY


def _k(Lpy, gap):
    """LS slope through origin of gap on Lpy."""
    d = float(np.sum(Lpy * Lpy))
    return float(np.sum(Lpy * gap) / d) if d > 0 else float("nan")


def compute():
    Sp = PY.load_series()
    pp, _ = PY.fit(Sp)
    Lp_full, _ = PY.simulate(Sp, *pp)
    Sp0 = dict(Sp)
    Sp0["yb_val"] = np.zeros_like(Sp["yb_val"]); Sp0["yb_lm_val"] = np.zeros_like(Sp["yb_lm_val"])
    Lp_base, _ = PY.simulate(Sp0, *pp)            # pyUSD without the YB campaigns

    So = OT.load_series()
    po, _ = OT.fit(So)
    Lo_model, _ = OT.simulate(So, *po)            # peers, own incentive only (no competition)
    Lo_meas = So["L"]

    Lpy = np.interp(So["t"], Sp["t"], Sp["L"])    # pyUSD TVL on the peers' grid
    Lpy_gain = np.interp(So["t"], Sp["t"], np.clip(Sp["L"] - Lp_base, 0, None))
    gap = Lo_model - Lo_meas                       # peers below their own-incentive model

    tt = So["t"].astype("datetime64[s]")
    on = So["py_on"]
    k_all = _k(Lpy, gap)                            # vs total pyUSD TVL, full series
    k_on = _k(Lpy[on], gap[on])                     # vs total pyUSD TVL, while incentivised
    k_gain = _k(Lpy_gain, gap)                      # vs incentive-DRIVEN pyUSD TVL only
    return dict(So=So, Sp=Sp, Lo_model=Lo_model, Lo_meas=Lo_meas, Lpy=Lpy,
                Lp_base=Lp_base, gap=gap, on=on, tt=tt,
                k_all=k_all, k_on=k_on, k_gain=k_gain, pp=pp, po=po)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()
    R = compute()
    k = R["k_all"]
    print(f"pyUSD params (tin,tout,xlo,xhi,p_in) = {', '.join(f'{x:.2f}' for x in R['pp'])}")
    print(f"others params (tin,tout,xlo,xhi)     = {', '.join(f'{x:.2f}' for x in R['po'])}")
    print(f"leakage k = % of pyUSD TVL that must be added back to peers to match their model:")
    print(f"  k (vs total pyUSD TVL, full series)      = {R['k_all']*100:.0f}%  "
          f"→ net efficiency {(1-R['k_all'])*100:.0f}%")
    print(f"  k (vs total pyUSD TVL, while incentivised)= {R['k_on']*100:.0f}%")
    print(f"  k (vs incentive-DRIVEN pyUSD TVL)         = {R['k_gain']*100:.0f}%")

    import matplotlib
    matplotlib.use("Agg" if args.save else "QtAgg")
    import matplotlib.pyplot as plt
    tt, So, Sp = R["tt"], R["So"], R["Sp"]
    ttp = Sp["t"].astype("datetime64[s]")
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 9), sharex=True)

    def shade(ax):
        for j, (s, e) in enumerate(OT._intervals(So["t"], R["on"])):
            ax.axvspan(np.datetime64(int(s), "s"), np.datetime64(int(e), "s"), color="orange",
                       alpha=0.12, label="pyUSD incentivised" if j == 0 else None)

    ax1.plot(tt, R["Lo_meas"] / 1e6, "k", lw=1.6, label="peers measured TVL")
    ax1.plot(tt, R["Lo_model"] / 1e6, "steelblue", ls="--", lw=1.4, label="peers own-incentive model (no competition)")
    ax1.plot(tt, (R["Lo_meas"] + k * R["Lpy"]) / 1e6, "seagreen", lw=1.4,
             label=f"measured + k·pyUSD TVL  (k = {k*100:.0f}%)")
    shade(ax1)
    ax1.set_ylabel("peers staked TVL [$M]"); ax1.grid(alpha=0.3); ax1.legend(fontsize=8, loc="upper right")
    ax1.set_title(f"Adding back {k*100:.0f}% of pyUSD TVL reconciles the peers with their own-incentive model "
                  f"→ pyUSD net efficiency ≈ {(1-k)*100:.0f}%")

    ax2.plot(ttp, Sp["L"] / 1e6, "crimson", lw=1.4, label="pyUSD measured TVL")
    ax2.plot(ttp, R["Lp_base"] / 1e6, "crimson", ls=":", lw=1.1, label="pyUSD without YB (counterfactual)")
    ax2.plot(tt, R["gap"] / 1e6, "purple", lw=1.3, label="peers gap = model − measured (cannibalised)")
    shade(ax2)
    ax2.set_ylabel("TVL [$M]"); ax2.grid(alpha=0.3); ax2.legend(fontsize=8, loc="upper right")
    ax2.set_title("Driver (pyUSD TVL) vs the peer TVL gap it explains")
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=120); print(f"saved {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
