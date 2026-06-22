#!/usr/bin/env python3
"""crvUSD TVL decomposition + net incentives to the NON-pyUSD pools.

Different crvUSD pools carry different risk premiums, so a single combined fit is
apples-and-oranges. Instead this is descriptive: it shows the staked-TVL split
(pyUSD vs others vs aggregate) and, separately, the **net incentives to the
non-pyUSD pools** — CRV emissions + their StakeDAO/Votemarket YB (the biweekly pYB
campaigns to gauges 0x2280/0x4e6b/0x95f0 since Oct-2025, which the Curve-gauge
reward_data misses; NOT Merkl). The point: the other pools' TVL tracks *their own*
incentives, so pyUSD's rush isn't draining them (no rotation).

Usage: uv run python plot_crvusd_others.py --save pics/crvusd_others.png
"""
import argparse
import csv
import datetime as dt
import lzma
from pathlib import Path

import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
YEAR = 365.25 * 86400.0
PYUSD_GAUGE = "0xf69fb60b79e463384b40dbfdfb633ab5a863c9a2"

# pyUSD's end-April campaign (1435) is the ONLY pYB campaign with an IncentiveGaugeHook
# (hook != 0x0), i.e. the only one whose YB actually reaches LPs. Of its 800k YB, ~663k
# was hook-claimed to LPs over these two epochs (= voting window + 1 week); the rest went
# to voters. Same LP amounts as fit_pool_dynamics.py. Every OTHER pYB campaign has
# hook == 0x0 → a plain Votemarket bribe paid to VOTERS, not LPs, so it is NOT a direct
# LP incentive (its only effect on the pool is the extra CRV it buys, already counted).
PYUSD_LP_YB = [("2026-04-16", "2026-04-23", 331_533.0),
               ("2026-04-23", "2026-04-30", 331_874.0)]


def load():
    with lzma.open(HERE / "crvusd_pools.csv.xz") as fh:
        ca = pl.read_csv(fh.read())
    g = lambda c: ca[c].cast(pl.Float64, strict=False).to_numpy()
    t = g("timestamp")
    agg_tvl = g("staked_tvl")
    crv_v_all = g("crv_rate") * g("sum_rel_weight") * g("crv_price") * YEAR
    yb_gauge_all = g("yb_sum_rate") * g("yb_price") * YEAR
    crv_rate, crv_px, yb_px = g("crv_rate"), g("crv_price"), g("yb_price")
    sum_rw = g("sum_rel_weight")

    with lzma.open(HERE / "pool_apr.csv.xz") as fh:
        pa = pl.read_csv(fh.read())
    tp = pa["timestamp"].to_numpy()
    py_tvl = np.interp(t, tp, (pa["gauge_staked"].cast(pl.Float64) * pa["virtual_price"].cast(pl.Float64)).to_numpy())
    py_rw = np.interp(t, tp, pa["crv_rel_weight"].cast(pl.Float64).to_numpy())
    py_ybrate = pa["yb_rate"].cast(pl.Float64).to_numpy() * (tp < pa["yb_period_finish"].cast(pl.Float64).to_numpy())
    py_yb_gauge = np.interp(t, tp, py_ybrate) * yb_px * YEAR

    def _epoch_value(start, end, amount_yb):
        """Annualised $ value of `amount_yb` spread evenly over [start, end)."""
        s = int(dt.datetime.fromisoformat(start).replace(tzinfo=dt.UTC).timestamp())
        e = int(dt.datetime.fromisoformat(end).replace(tzinfo=dt.UTC).timestamp())
        out = np.zeros(t.size)
        if e > s:
            out[(t >= s) & (t < e)] = amount_yb / (e - s)           # YB/s
        return out

    # pyUSD LP incentive: only the hook-routed LP portion (663k), over the +1-week epochs.
    vm_py_lp = sum(_epoch_value(a, b, q) for a, b, q in PYUSD_LP_YB) * yb_px * YEAR

    # Non-pyUSD pYB campaigns are voter BRIBES (hook == 0x0) — paid to voters, not LPs.
    # Tracked only as a reference line; NOT part of the LP-incentive stack.
    vm_others_bribe = np.zeros(t.size)
    for c in csv.DictReader(open(HERE / "votemarket_yb_campaigns.csv")):
        if c["gauge"].lower() == PYUSD_GAUGE or int(c["hook"], 16) != 0:
            continue                                                # skip pyUSD & hooked
        s = int(dt.datetime.fromisoformat(c["voting_start"]).replace(tzinfo=dt.UTC).timestamp()) + 7 * 86400
        e = int(dt.datetime.fromisoformat(c["voting_end"]).replace(tzinfo=dt.UTC).timestamp()) + 7 * 86400
        if e > s:
            vm_others_bribe[(t >= s) & (t < e)] += float(c["total_yb"]) / (e - s)
    vm_others_bribe *= yb_px * YEAR

    # All incentives as VALUE ($/yr) — never relative weight.
    crv_py = crv_rate * np.clip(py_rw, 0, None) * crv_px * YEAR
    crv_others = crv_rate * np.clip(sum_rw - py_rw, 0, None) * crv_px * YEAR
    ybgauge_others = np.clip(yb_gauge_all - py_yb_gauge, 0, None)
    return dict(t=t, agg=agg_tvl, py=py_tvl, others=agg_tvl - py_tvl,
                crv_others=crv_others, ybgauge_others=ybgauge_others, vm_others_bribe=vm_others_bribe,
                crv_py=crv_py, ybgauge_py=np.clip(py_yb_gauge, 0, None), vm_py_lp=vm_py_lp)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()
    S = load()
    tt = S["t"].astype("datetime64[s]")
    inc_others = S["crv_others"] + S["ybgauge_others"]          # LP incentive: CRV + on-gauge only
    inc_py = S["crv_py"] + S["ybgauge_py"] + S["vm_py_lp"]

    import matplotlib
    matplotlib.use("Agg" if args.save else "QtAgg")
    import matplotlib.pyplot as plt
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(13, 11), sharex=True)

    ax1.plot(tt, S["agg"] / 1e6, "k", lw=1.5, label="aggregate staked (all crvUSD stable pools)")
    ax1.plot(tt, S["py"] / 1e6, "crimson", lw=1.3, label="pyUSD staked")
    ax1.plot(tt, S["others"] / 1e6, "steelblue", lw=1.3, label="others = aggregate − pyUSD")
    ax1.set_ylabel("staked TVL [$M]"); ax1.legend(fontsize=8); ax1.grid(alpha=0.3)
    ax1.set_title("crvUSD staked TVL decomposition, and the VALUE of incentives reaching LPs")

    ax2.stackplot(tt, S["crv_others"] / 1e6, S["ybgauge_others"] / 1e6,
                  labels=["CRV emissions", "on-gauge YB"], colors=["#9ecae1", "#a1d99b"])
    ax2.plot(tt, inc_others / 1e6, "k", lw=1.2, label="total LP incentive")
    ax2.plot(tt, S["vm_others_bribe"] / 1e6, "--", color="#e6550d", lw=1.1,
             label="Votemarket YB → voters (vote bribe, NOT an LP reward;\nits effect is already inside CRV emissions)")
    ax2.set_ylabel("non-pyUSD incentive [$M/yr]"); ax2.legend(fontsize=8, loc="upper right")
    ax2.set_title("Net incentive VALUE reaching LPs of the non-pyUSD crvUSD pools"); ax2.grid(alpha=0.3)

    ax3.stackplot(tt, S["crv_py"] / 1e6, S["vm_py_lp"] / 1e6, S["ybgauge_py"] / 1e6,
                  labels=["CRV emissions", "StakeDAO/Votemarket YB → LPs (hooked)", "on-gauge YB"],
                  colors=["#9ecae1", "#fdae6b", "#a1d99b"])
    ax3.plot(tt, inc_py / 1e6, "k", lw=1.2, label="total LP incentive")
    ax3.set_ylabel("pyUSD incentive [$M/yr]"); ax3.legend(fontsize=8, loc="upper right")
    ax3.set_title("pyUSD incentive VALUE reaching LPs — incl. the hook-routed end-April campaign (663k YB to LPs)")
    ax3.grid(alpha=0.3)
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=120); print(f"saved {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
