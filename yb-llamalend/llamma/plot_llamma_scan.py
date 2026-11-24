"""Loss and liquidation discount vs A from llamma_scan, with the parameters they imply.

Reads a llamma_scan CSV (one row per A), plots loss and discount against A for the worst window and the mean of the
worst windows, and replays the worst window at the optimal A (llamma_scan --trace) to show what happened in it.
Parameters as in curvefi/llamma-simulator(_v2) (README, docs/parameter_selection):
  optimal A             the minimum of the discount curve (worst 0.01% of windows). Where the curve is flat, the
                        smallest A within --plateau-tol (0.1% of collateral value) of the minimum: the same LTV within
                        that tolerance, wider bands and a smaller liquidation discount. --plateau-tol 0: the argmin.
  liquidation_discount  the loss at that A
  loan_discount         liquidation_discount + margin (0.015 by default: WETH is borrowed against yb-WETH, a
                        stable pair; curvefi uses 0.03 for collateral against USD)
  LTV_max               1 - (discount at that A + margin)

usage: uv run --project ../curve-fx-sim/curve-fx-optimization --no-sync python plot_llamma_scan.py
           ../runs/weth-live-pyth-60s/llamma_scan_A.csv ../runs/weth-live-pyth-60s/llamma_input.bin [--margin 0.015]
           [--plateau-tol 0.001]
"""

import argparse
import csv
import datetime as dt
import subprocess
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

HERE = Path(__file__).resolve().parent
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
LOSS, DISCOUNT, ORACLE, MARKET, AMM = "#2a78d6", "#eb6834", "#6250d6", "#1baf7a", "#eda100"


def read_csv(path):
    with open(path, newline="") as stream:
        rows = list(csv.DictReader(stream))
    return {k: np.array([float(r[k]) for r in rows]) for k in rows[0]}


def utc(ts):
    return dt.datetime.fromtimestamp(int(ts), dt.UTC)


def main(scan_path: Path, input_path: Path, margin: float, plateau_tol: float) -> None:
    s = read_csv(scan_path)
    A = s["A"]
    bands, fee, ext_fee, top = int(s["bands"][0]), s["fee"][0], s["ext_fee"][0], int(s["top"][0])
    windows = int(s["windows"][0])
    k = int(np.argmin(s["discount_topN"]))
    plateau = np.flatnonzero(s["discount_topN"] <= s["discount_topN"][k] + plateau_tol)
    i = int(plateau[0])  # the smallest A on the plateau (the argmin when plateau_tol = 0)
    a_opt, disc_min, loss_opt = int(A[i]), s["discount_topN"][i], s["loss_topN"][i]
    j = int(np.argmin(s["discount_top1"]))
    liq, loan, ltv = loss_opt, loss_opt + margin, 1 - (disc_min + margin)
    print(f"discount minimum at A {int(A[k])}: {s['discount_topN'][k]:.4f} (loss {s['loss_topN'][k]:.4f}); within "
          f"{plateau_tol:.2%} of it from A {int(A[plateau[0]])} to {int(A[plateau[-1]])}")
    print(f"chosen A {a_opt}: discount {disc_min:.4f}, loss {loss_opt:.4f} (worst {top} of {windows} windows)")
    print(f"liquidation_discount {liq:.4f}, loan_discount {loan:.4f}, LTV_max {ltv:.4f} "
          f"(max leverage {1 / (1 - ltv):.1f}x)")
    print(f"worst single window: discount minimum at A {int(A[j])}: {s['discount_top1'][j]:.4f} "
          f"(loss {s['loss_top1'][j]:.4f}); LTV_max {1 - (s['discount_top1'][j] + margin):.4f}")

    # The worst window at the optimal A, replayed with a per-row trace
    trace_path = scan_path.with_name(f"llamma_worst_A{a_opt}.csv")
    subprocess.run([str(HERE / "build" / "llamma_scan"), str(input_path), "/dev/null", "--bands", str(bands),
                    "--fee", f"{fee:.10g}", "--ext-fee", f"{ext_fee:.10g}", "--trace", str(int(s["worst_start_ts"][i])),
                    str(a_opt), str(trace_path)], check=True)
    w = read_csv(trace_path)
    when = [utc(t) for t in w["timestamp"]]
    p0 = w["oracle"][0]

    plt.rcParams.update({"font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2,
                         "ytick.color": INK2, "axes.titlecolor": INK, "axes.titlesize": 10,
                         "axes.titlelocation": "left", "legend.frameon": False, "lines.linewidth": 1.5})
    fig = plt.figure(figsize=(12, 10), facecolor=SURFACE)
    gs = fig.add_gridspec(3, 1, height_ratios=[2.4, 1.3, 1.0])
    ax = [fig.add_subplot(gs[0])]
    ax += [fig.add_subplot(gs[1]), fig.add_subplot(gs[2])]
    ax[2].sharex(ax[1])
    for a in ax:
        a.set_facecolor(SURFACE)
        a.grid(True, color=GRID, linewidth=0.6)
        for side in ("top", "right"):
            a.spines[side].set_visible(False)

    a = ax[0]
    a.plot(A, 100 * s["loss_topN"], color=LOSS, marker="o", markersize=4, label=f"loss, worst 0.01% ({top} windows)")
    a.plot(A, 100 * s["discount_topN"], color=DISCOUNT, marker="o", markersize=4,
           label="discount = 1 - (1 - loss) x band recovery")
    a.plot(A, 100 * s["loss_top1"], color=LOSS, linestyle="--", linewidth=1, label="loss, worst window")
    a.plot(A, 100 * s["discount_top1"], color=DISCOUNT, linestyle="--", linewidth=1, label="discount, worst window")
    if len(plateau) > 1:
        a.axvspan(A[plateau[0]], A[plateau[-1]], color=GRID, alpha=0.6, linewidth=0,
                  label=f"discount within {100 * plateau_tol:.1f}% of its minimum")
    a.axvline(a_opt, color=INK2, linewidth=0.8)
    a.annotate(f"A = {a_opt}\ndiscount {100 * disc_min:.2f}%\nloss {100 * loss_opt:.2f}%",
               xy=(a_opt, 100 * disc_min), xytext=(12, 40), textcoords="offset points", color=INK,
               arrowprops={"arrowstyle": "-", "color": INK2, "linewidth": 0.8})
    a.set_xscale("log")
    a.set_xlabel("A")
    a.set_ylabel("% of collateral value")
    a.set_ylim(0, min(100 * max(s["discount_topN"].max(), s["discount_top1"].max()) * 1.05,
                      100 * 4 * disc_min + 5))
    a.set_title(f"yb-WETH collateral, WETH borrowed: LLAMMA loss and discount vs A ({bands} bands, fee {fee:.2%}, "
                f"external fee {ext_fee:.2%}, 30-60 min windows)\n"
                f"At A = {a_opt}: liquidation_discount {100 * liq:.2f}%, loan_discount {100 * loan:.2f}% "
                f"(+{margin:.1%}), LTV_max {100 * ltv:.1f}% (leverage {1 / (1 - ltv):.0f}x). "
                f"Worst single window: lowest discount {100 * s['discount_top1'][j]:.2f}% at A = {int(A[j])}")
    a.legend(loc="upper right")

    a = ax[1]
    a.plot(when, w["oracle"], color=ORACLE, label="oracle (weth.oracles-ll)")
    a.plot(when, w["market"], color=MARKET, label="market (redemption value)")
    a.plot(when, w["amm_price"], color=AMM, linewidth=1, label="LLAMMA price")
    a.set_ylabel("WETH per LT")
    a.set_title(f"Worst window at A = {a_opt}: {when[0]:%Y-%m-%d %H:%M} .. {when[-1]:%H:%M} UTC")
    a.legend(loc="best")
    plt.setp(a.get_xticklabels(), visible=False)

    a = ax[2]
    a.plot(when, 100 * (w["x_down"] / p0), color=DISCOUNT, label="position x_down / (1 LT at the opening oracle)")
    a.plot(when, 100 * w["y"], color=LOSS, linewidth=1, label="collateral left, % of 1 LT")
    a.set_ylabel("%")
    a.legend(loc="best")
    a.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    fig.tight_layout()
    out = scan_path.with_suffix(".png")
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    print("wrote", out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("scan", type=Path)
    parser.add_argument("input", type=Path)
    parser.add_argument("--margin", type=float, default=0.015)
    parser.add_argument("--plateau-tol", type=float, default=0.001)
    args = parser.parse_args()
    main(args.scan.resolve(), args.input.resolve(), args.margin, args.plateau_tol)
