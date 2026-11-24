"""One yb-WETH trajectory in WETH per LT: the YB lending oracles and the redemption value, for LlamaLend.

Reads a Shift-click replay (fxopt shiftclick ... --output DIR) and the block tape it ran on, writes DIR/../trajectory.png
and DIR/../trajectory.csv (one row per trace row, 300 s with run.sh). At every row, from the simulated pool and LevAMM
(yb_prices.py, checked against the deployed contracts by check_oracle_onchain.py):

  oracle_weth       weth.oracles.yieldbasis.eth: YBLendingOracle.price_in_asset, the LT valued where arbitrage takes
                    the LevAMM with the LP priced at the pool's price_oracle (projected to the row time, as the pool's
                    view does), per LT after the admin / staker split
  oracle_ll_weth    weth.oracles-ll.yieldbasis.eth: the same with its EMA (ema_time 866 s) on the fundamental rate,
                    checkpointed (price_w) at every row
  redemption_weth   LT.emergency_withdraw of one LT: its share of the LP's WETH and crvUSD minus its share of the debt,
                    the net crvUSD converted to WETH at the market price (Binance ETH in crvUSD, the tape's arb_price)
  pps_weth          LT.pricePerShare: the LT's own value at price_scale

LT accounting: the harness has no LT, so LT._calculate_values runs at every row on the LevAMM value at price_scale,
from a fresh LT (1 WETH per LT) with the live split: min_admin_fee 10% and 44.3% of LT staked (2026-11-24), no
deposits, withdrawals or new staking. Stakers only recover losses; their profit share is moved to the other holders by
burning staked LT, so an unstaked LT grows faster than the LevAMM's equity.

usage: uv run --project curve-fx-sim/curve-fx-optimization --no-sync python plot_trajectory.py runs/weth-live-pyth/trace
           [--min-admin-fee 0.1] [--staked-fraction 0.443]
"""

import argparse
import csv
import datetime as dt
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

import yb_prices as yp  # noqa: E402

LL_EMA_TIME = 866  # s, YBLendingOracleLLFactory default_ema_time, as the WETH clone reads
MIN_ADMIN_FEE = 0.1  # yb Factory.min_admin_fee, 2026-11-24
STAKED_FRACTION = 0.4434  # yb-WETH LT staked in its gauge / supply, 2026-11-24 (3817.95 / 8610.18)

# Reference palette (dataviz skill), light mode: categorical slots in fixed order, one per entity
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
ETH, SCALE, REDEEM, ORACLE, ORACLE_LL = "#2a78d6", "#eb6834", "#1baf7a", "#6250d6", "#e87ba4"


def load(trace_dir: Path):
    summary = json.loads((trace_dir / "shiftclick.json").read_text())
    session = summary["replay"]["open_session"]
    trace_path = Path(summary["result"]["artifacts"]["trace_path"])
    if not trace_path.is_absolute():
        trace_path = trace_dir / trace_path
    rows = json.loads(trace_path.read_text())
    keys = ("t", "price_scale", "price_oracle", "last_prices", "last_timestamp", "token0", "token1", "D",
            "total_supply", "vp", "yb_debt", "yb_collateral_lp", "yb_lp_oracle", "yb_stable_aggregator", "fee")
    tr = {k: np.array([r[k] for r in rows], dtype=float) for k in keys}
    with np.load(session["block_tape_path"]) as tape:
        t0, block_s, arb = int(tape["t0"]), int(tape["block_s"]), tape["arb_price"]
    k = ((tr["t"].astype(np.int64) - t0) // block_s).astype(np.int64)
    assert np.all((tr["t"].astype(np.int64) - t0) % block_s == 0), "trace rows off the tape's slots"
    tr["eth_crvusd"] = arb[k]
    return summary, tr


def lt_prices(tr, pool_A, ma_time, min_admin_fee, staked_fraction):
    """The four per-LT series in WETH, plus the redemption constituents."""
    # The pool's price_oracle() view: the cached EMA advanced to now towards last_prices, capped to [ps / 2, 2 ps]
    capped = np.clip(tr["last_prices"], tr["price_scale"] / 2, 2 * tr["price_scale"])
    alpha = np.exp(-np.maximum(tr["t"] - tr["last_timestamp"], 0) / ma_time)
    po = capped * (1 - alpha) + tr["price_oracle"] * alpha

    terms = yp.oracle_terms(po, tr["price_scale"], tr["vp"], tr["D"], tr["total_supply"], yp.a_raw(pool_A),
                            tr["yb_collateral_lp"], tr["yb_debt"], tr["yb_stable_aggregator"])
    lp_oracle = 2 * tr["vp"] * np.sqrt(tr["price_scale"]) * tr["yb_stable_aggregator"]
    mismatch = np.max(np.abs(lp_oracle / tr["yb_lp_oracle"] - 1))
    print(f"LP oracle from the trace's pool fields vs the harness' yb_lp_oracle: max relative difference {mismatch:.1e}")
    assert mismatch < 1e-9, "trace fields do not reproduce the harness' LP oracle"

    cur_value = terms.value_ps / terms.p_o_ps  # LevAMM value in WETH at price_scale, what LT accounts with
    lv = yp.LiquidityState(admin=0.0, total=cur_value[0], ideal_staked=staked_fraction * cur_value[0],
                           staked=staked_fraction * cur_value[0], supply_tokens=cur_value[0],
                           staked_tokens=staked_fraction * cur_value[0])
    share, pps, staked = (np.empty(len(cur_value)) for _ in range(3))
    for i, v in enumerate(cur_value):  # a checkpoint per row
        lv = yp.calculate_values(lv, float(v), min_admin_fee)
        share[i], pps[i], staked[i] = yp.per_share(lv), lv.total / lv.supply_tokens, lv.staked_tokens / lv.supply_tokens

    oracle = terms.equity_po * share / terms.asset_price
    fundamental = terms.value_ps * share / terms.p_o_ps
    oracle_ll = np.where(terms.tradable, oracle * yp.ll_ema(fundamental, tr["t"], LL_EMA_TIME) / fundamental, oracle)
    coin1, coin0, redemption = yp.redemption(tr["yb_collateral_lp"], tr["yb_debt"], tr["token0"], tr["token1"],
                                             tr["total_supply"], share, tr["eth_crvusd"])
    return dict(price_oracle=po, oracle_weth=oracle, oracle_ll_weth=oracle_ll, redemption_weth=redemption,
                redeem_weth=coin1, redeem_crvusd=coin0, pps_weth=pps, staked_fraction=staked,
                tradable=terms.tradable.astype(int))


def gap_stats(gap):
    q = np.quantile(gap, [0.01, 0.5, 0.99])
    return (f"median {100 * q[1]:+.2f}%, 1-99% {100 * q[0]:+.2f}% .. {100 * q[2]:+.2f}%, "
            f"min {100 * gap.min():+.1f}%, max {100 * gap.max():+.1f}%")


def main(trace_dir: Path, min_admin_fee: float, staked_fraction: float) -> None:
    summary, tr = load(trace_dir)
    metrics, session = summary["result"]["metrics"], summary["replay"]["open_session"]
    inputs = summary["result"]["artifacts"]["effective_inputs"]
    ok = np.isfinite(tr["eth_crvusd"]) & (tr["yb_collateral_lp"] > 0)
    tr = {k: v[ok] for k, v in tr.items()}
    when = np.array([dt.datetime.fromtimestamp(int(t), dt.UTC) for t in tr["t"]])

    px = lt_prices(tr, inputs["pool.A"], inputs["pool.ma_time"], min_admin_fee, staked_fraction)
    gap = px["oracle_weth"] / px["redemption_weth"] - 1
    gap_ll = px["oracle_ll_weth"] / px["redemption_weth"] - 1
    scale_gap = tr["price_scale"] / tr["eth_crvusd"] - 1
    oracle_gap = px["price_oracle"] / tr["eth_crvusd"] - 1
    dtv = tr["yb_debt"] / (tr["yb_collateral_lp"] * tr["yb_lp_oracle"] / tr["yb_stable_aggregator"])
    years = (tr["t"][-1] - tr["t"][0]) / (365 * 86400)
    growth = px["redemption_weth"][-1] / px["redemption_weth"][0]
    print(f"redemption {px['redemption_weth'][0]:.4f} -> {px['redemption_weth'][-1]:.4f} WETH per LT "
          f"({growth ** (1 / years) - 1:.2%} a year); staked LT {staked_fraction:.1%} -> {px['staked_fraction'][-1]:.1%}")
    print(f"oracle / redemption - 1:    {gap_stats(gap)}")
    print(f"LL oracle / redemption - 1: {gap_stats(gap_ll)}")
    for th in (0.01, 0.02, 0.05):
        print(f"  |gap| > {th:.0%}: oracle {np.mean(np.abs(gap) > th):.2%} of the time, LL {np.mean(np.abs(gap_ll) > th):.2%}")

    plt.rcParams.update({"font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2,
                         "ytick.color": INK2, "axes.titlecolor": INK, "axes.titlesize": 10,
                         "axes.titlelocation": "left", "legend.frameon": False, "lines.linewidth": 1.2})
    fig, ax = plt.subplots(5, 1, figsize=(12, 14), sharex=True, facecolor=SURFACE,
                           gridspec_kw={"height_ratios": [2.0, 2.2, 1.8, 1.4, 1.2]})
    for a in ax:
        a.set_facecolor(SURFACE)
        a.grid(True, color=GRID, linewidth=0.6)
        for side in ("top", "right"):
            a.spines[side].set_visible(False)

    a = ax[0]
    a.plot(when, tr["eth_crvusd"], color=ETH, label="ETH (Binance, in crvUSD)")
    a.plot(when, tr["price_scale"], color=SCALE, label="pool price_scale")
    a.set_yscale("log")
    a.set_ylabel("crvUSD per ETH")
    a.set_title("ETH and the yb-WETH pool's price_scale (live YBOraclizedPolicy, Pyth ETH/USD)")
    a.legend(loc="upper right")

    a = ax[1]
    a.plot(when, px["redemption_weth"], color=REDEEM, label="redemption value (emergency_withdraw at market)")
    a.plot(when, px["oracle_weth"], color=ORACLE, linewidth=0.8, label="weth.oracles.yieldbasis.eth")
    a.plot(when, px["oracle_ll_weth"], color=ORACLE_LL, linewidth=0.8, label="weth.oracles-ll (EMA 866 s)")
    a.set_ylabel("WETH per LT")
    a.set_title(f"yb-WETH LT in WETH: redemption {px['redemption_weth'][0]:.3f} -> {px['redemption_weth'][-1]:.4f} "
                f"({growth ** (1 / years) - 1:.2%} a year; admin fee {min_admin_fee:.0%} min, "
                f"{staked_fraction:.0%} staked at start)")
    a.legend(loc="upper left")

    a = ax[2]
    a.plot(when, 100 * gap, color=ORACLE, linewidth=0.8, label="weth.oracles.yieldbasis.eth")
    a.plot(when, 100 * gap_ll, color=ORACLE_LL, linewidth=0.8, label="weth.oracles-ll")
    a.axhline(0, color=INK2, linewidth=0.8)
    a.set_ylabel("oracle / redemption - 1, %")
    a.set_title(f"Oracle vs redemption value: {gap_stats(gap)}")
    a.legend(loc="lower left")

    a = ax[3]
    a.plot(when, 100 * scale_gap, color=SCALE, linewidth=0.8, label="price_scale")
    a.plot(when, 100 * oracle_gap, color=ETH, linewidth=0.8, label="price_oracle (pool EMA)")
    a.axhline(0, color=INK2, linewidth=0.8)
    a.set_ylabel("vs ETH, %")
    a.set_title(f"Pool prices vs the market: worst 7-day mean |price_scale gap| {metrics['max_7d_rel_price_diff']:.2%}")
    a.legend(loc="lower left")

    a = ax[4]
    a.plot(when, dtv, color=REDEEM)
    a.axhline(0.5, color=INK2, linewidth=0.8)
    a.set_ylabel("debt / collateral")
    a.set_title(f"LevAMM debt to collateral at the LP oracle (0.5 at 2x): {dtv.min():.3f} .. {dtv.max():.3f}, "
                f"{metrics['yb_releverage_trades']:.0f} releverage fills at {session['yb_releverage_fee']:.1%} fee")
    a.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 4, 7, 10)))
    a.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

    fig.suptitle(f"yb-WETH, one historical trajectory {when[0]:%Y-%m-%d} .. {when[-1]:%Y-%m-%d} "
                 f"(curve-fx-sim, active_2l YieldBasis model)", x=0.01, ha="left", color=INK, fontsize=12)
    fig.tight_layout()
    out = trace_dir.parent / "trajectory.png"
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    print("wrote", out)

    csv_path = trace_dir.parent / "trajectory.csv"
    columns = {"eth_crvusd": tr["eth_crvusd"], "price_scale": tr["price_scale"], **px, "levamm_dtv": dtv,
               "pool_fee": tr["fee"], "crvusd_aggregator": tr["yb_stable_aggregator"]}
    with csv_path.open("w", newline="") as stream:
        w = csv.writer(stream)
        w.writerow(["timestamp", "utc", *columns])
        for i in range(len(tr["t"])):
            w.writerow([int(tr["t"][i]), f"{when[i]:%Y-%m-%d %H:%M:%S}", *(f"{v[i]:.10g}" for v in columns.values())])
    print("wrote", csv_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("trace_dir", type=Path)
    parser.add_argument("--min-admin-fee", type=float, default=MIN_ADMIN_FEE)
    parser.add_argument("--staked-fraction", type=float, default=STAKED_FRACTION)
    args = parser.parse_args()
    main(args.trace_dir.resolve(), args.min_admin_fee, args.staked_fraction)
