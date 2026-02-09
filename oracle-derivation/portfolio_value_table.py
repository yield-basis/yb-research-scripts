#!/usr/bin/env python3
"""
Compute a table of StableSwap portfolio values V = x0 + p*x1
for D=1.0, A=10, with p ranging from 0.1 to 10.0.
"""

from portfolio_value_solver import portfolio_value, WAD, A_MULTIPLIER, N_COINS

D = WAD  # 1.0
A = 10
_amp = A * N_COINS * A_MULTIPLIER  # 200000

P_VALUES = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
            1.2, 1.4, 1.6, 1.8, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]


def compute_table():
    rows = []
    for p_float in P_VALUES:
        p = int(p_float * WAD)
        V, iters = portfolio_value(D, p, _amp)
        V_float = V / WAD
        rows.append((p_float, V_float, V_float, p_float**0.5, iters))
    return rows


def print_table(rows):
    print(f"D = 1.0, A = {A}, _amp = {_amp}")
    print()
    print(f"{'p':>8s}  {'V':>14s}  {'V/D':>10s}  {'sqrt(p)':>10s}  {'iters':>5s}")
    print("-" * 55)
    for p_float, V_float, VD, sqrtp, iters in rows:
        print(f"{p_float:8.1f}  {V_float:14.10f}  {VD:10.6f}  {sqrtp:10.6f}  {iters:5d}")


def write_markdown(rows, filename="portfolio_value_table.md"):
    with open(filename, "w") as f:
        f.write(f"# Portfolio Value Table\n\n")
        f.write(f"**Parameters:** $D = 1.0$, $A = 10$, `_amp = {_amp}`\n\n")
        f.write(f"| $p$ | $V$ | $V/D$ | $\\sqrt{{p}}$ | iters |\n")
        f.write(f"|----:|----:|------:|------:|------:|\n")
        for p_float, V_float, VD, sqrtp, iters in rows:
            f.write(f"| {p_float:.1f} | {V_float:.10f} | {VD:.6f} | {sqrtp:.6f} | {iters} |\n")
    print(f"\nMarkdown table written to {filename}")


if __name__ == "__main__":
    rows = compute_table()
    print_table(rows)
    write_markdown(rows)
