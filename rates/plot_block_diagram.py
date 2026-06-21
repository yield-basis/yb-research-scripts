#!/usr/bin/env python3
"""Control block diagram of the scrvUSD incentive controller (PID + feed-forward).

Drawn in the classic control-systems style: summing junctions (Σ with +/−), gain
blocks, an integrator Ki/s and derivative Kd·s, the offer/actuator map, and the
depositor dynamics as a first-order plant 1/(τs+1). Renders to a PNG.

Usage
-----
    uv run python plot_block_diagram.py --save pics/incentive_block_diagram.png
"""
import argparse

import matplotlib
if "--save" in __import__("sys").argv:
    matplotlib.use("Agg")
else:
    try:
        matplotlib.use("QtAgg")
    except Exception:
        pass
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Circle


def box(ax, x, y, w, h, text, fc="#eef4fb", ec="#27496d", fs=9):
    ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                fc=fc, ec=ec, lw=1.4, zorder=3))
    ax.text(x, y, text, ha="center", va="center", fontsize=fs, zorder=4)


def summing(ax, x, y, r=0.28):
    ax.add_patch(Circle((x, y), r, fc="white", ec="black", lw=1.4, zorder=3))
    ax.text(x, y, "Σ", ha="center", va="center", fontsize=11, zorder=4)


def arrow(ax, x1, y1, x2, y2, label="", lpos=0.5, dy=0.18, color="black", fs=8.5):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="-|>", lw=1.3, color=color), zorder=2)
    if label:
        ax.text(x1 + (x2 - x1) * lpos, y1 + (y2 - y1) * lpos + dy, label,
                ha="center", va="center", fontsize=fs, color=color)


def sign(ax, x, y, s):
    ax.text(x, y, s, ha="center", va="center", fontsize=12, fontweight="bold",
            color="black", zorder=5)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--save", metavar="PNG")
    args = ap.parse_args()

    fig, ax = plt.subplots(figsize=(18, 8.5))
    ax.set_xlim(0, 19)
    ax.set_ylim(0, 9)
    ax.axis("off")

    Y = 4.6          # main signal line
    # --- input ---
    box(ax, 1.1, Y, 1.5, 0.9, "P(t)\nnet pressure\n(max 0, ·)", fc="#fdecea", ec="#b03a2e")
    Pt = 2.5         # P tap (fan-out up to FF/derivative)
    arrow(ax, 1.85, Y, 3.0, Y)
    ax.plot([Pt, Pt], [Y, 7.4], color="black", lw=1.1, zorder=1)  # vertical tap

    # --- error summing junction ---
    summing(ax, 3.35, Y)
    sign(ax, 3.05, Y + 0.36, "+")
    sign(ax, 3.05, Y - 0.40, "−")
    arrow(ax, 3.63, Y, 4.4, Y, "e = P − S", lpos=0.55, dy=0.22)
    et = 4.4         # error tap
    ax.plot([et, et], [3.0, Y], color="black", lw=1.1, zorder=1)

    # --- parallel controller paths (top→bottom): α (FF), Kd·s, Kp, Ki/s ---
    bx, bw = 6.0, 1.7
    yA, yD, yP, yI = 7.4, 6.2, Y, 3.0
    box(ax, bx, yA, bw, 0.8, "α   (feed-forward)\nα = 1.34", fc="#eafaf1", ec="#1e8449")
    box(ax, bx, yD, bw, 0.8, "Kd · s   (rising only)\nKd = 0.0122 yr", fc="#eafaf1", ec="#1e8449")
    box(ax, bx, yP, bw, 0.8, "Kp\nKp = 50")
    box(ax, bx, yI, bw, 0.9, "Ki / s   (clamp Imax)\nKi = 1610 /yr, Imax = 3.18")

    arrow(ax, Pt, yA, bx - bw / 2, yA)                 # P -> α
    arrow(ax, Pt, yD, bx - bw / 2, yD)                 # P -> Kd s
    arrow(ax, et, yP, bx - bw / 2, yP)                 # e -> Kp
    arrow(ax, et, yI, bx - bw / 2, yI)                 # e -> Ki/s

    # --- sum of controller paths -> S* ---
    Sx = 8.4
    summing(ax, Sx, Y)
    for yb in (yA, yD, yP, yI):
        ax.plot([bx + bw / 2, Sx - 0.05], [yb, yb], color="black", lw=1.1, zorder=1)
        ax.annotate("", xy=(Sx, Y), xytext=(Sx - 0.05, yb),
                    arrowprops=dict(arrowstyle="-|>", lw=1.2, color="black"), zorder=2)
    sign(ax, Sx - 0.42, Y + 0.30, "+")

    # --- clamp, offer map, market multiply (the actuator) ---
    arrow(ax, Sx + 0.3, Y, 9.5, Y, "S*", lpos=0.5, dy=0.22)
    box(ax, 10.25, Y, 1.4, 0.9, "clamp\n0 … S_cap\nS_cap = 16")
    arrow(ax, 10.95, Y, 11.6, Y)
    box(ax, 12.5, Y, 1.7, 1.0, "offer map\nx = 2 + S*/β\ncap x ≤ 34×\nβ = 0.5", fc="#fef9e7", ec="#b9770e")
    arrow(ax, 13.35, Y, 14.1, Y, "x", lpos=0.5, dy=0.22)
    mx = 14.45                          # multiply junction: x · m -> incentive APR
    ax.add_patch(Circle((mx, Y), 0.28, fc="white", ec="black", lw=1.4, zorder=3))
    ax.text(mx, Y, "×", ha="center", va="center", fontsize=13, zorder=4)
    box(ax, mx, 2.5, 2.0, 0.8, "market norm m\nAave 7-day EMA", fc="#f4ecf7", ec="#6c3483")
    arrow(ax, mx, 2.9, mx, Y - 0.3)

    # --- plant: depositor dynamics ---
    arrow(ax, mx + 0.28, Y, 15.5, Y, "(x−1)·m\nbonus APR", lpos=0.5, dy=0.5, fs=7.5)
    box(ax, 16.55, Y, 1.9, 1.1,
        "depositor plant\n1 / (τ s + 1)\nτin 9d / τout 4.5d", fc="#eaf2f8", ec="#1f618d")
    arrow(ax, 17.5, Y, 18.05, Y)
    ax.text(18.05, Y + 0.35, "S (sink)", ha="right", va="bottom", fontsize=9, fontweight="bold")

    # --- feedback S -> Σ1 (−) ---
    fb = 1.4
    ax.plot([17.8, 17.8], [Y, fb], color="black", lw=1.2, zorder=1)
    ax.plot([17.8, 3.35], [fb, fb], color="black", lw=1.2, zorder=1)
    ax.annotate("", xy=(3.35, Y - 0.28), xytext=(3.35, fb),
                arrowprops=dict(arrowstyle="-|>", lw=1.2, color="black"), zorder=2)
    ax.text(10.4, fb - 0.28, "S  (measured incremental sink, fed back)",
            ha="center", va="top", fontsize=8.5)

    # --- side outputs: spend & coverage ---
    box(ax, 12.4, 7.8, 3.4, 0.8, "spend  =  (x−1)·m · S   ≈ 0.13 %/yr of half-TVL",
        fc="#fdf2f8", ec="#a93226", fs=8.5)
    box(ax, 16.5, 7.8, 3.3, 0.9,
        "coverage:  S (+ 20% YB reserve)\nvs P  →  uncovered ≈ 0%", fc="#fdf2f8", ec="#a93226", fs=8.5)

    ax.set_title("scrvUSD incentive controller — PID + feed-forward block diagram",
                 fontsize=13)
    fig.tight_layout()
    if args.save:
        fig.savefig(args.save, dpi=120)
        print(f"saved {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
