"""Compare llamma_scan (phil-svg's byte-exact LLAMMA v2 engine) with curvefi/llamma-simulator_v2's LendingAMM.

For a few windows and values of A, replays the same rows (oracle, market) through both: llamma_scan --trace, and v2's
Simulator.calculate_loss with each row as a flat candle (open = high = low = close = market), the same fee, dynamic fee
multiplier 0.25 (the v2 AMM's built-in), external fee and 4 bands. Prints both losses.

usage: uv run --no-project --with numpy --with psutil python check_vs_simulator_v2.py
           ../runs/weth-live-pyth-60s/llamma_input.bin [START_TS ...]
"""

import csv
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "external" / "llamma-simulator_v2"))
from simulator.amm.intitial_liquidity import ConstantInitialLiquidity  # noqa: E402
from simulator.amm.simulator import Simulator  # noqa: E402

FEE, EXT_FEE, BANDS = 0.002, 5e-4, 4
# The 2025-10-10 crash, the 2026-08-20 rally, the 2026-02-06 sell-off and a quiet day
STARTS = [1760131211, 1787348411, 1770337811, 1750000031]
AS = [30, 100, 300, 1000]


def v2_loss(rows, A):
    prices = [(int(r["timestamp"]), *(float(r["market"]),) * 4, 0.0) for r in rows]
    oracle = [float(r["oracle"]) for r in rows]
    sim = Simulator.__new__(Simulator)  # no price loader: calculate_loss takes the rows directly
    sim.initial_liquidity_class, sim.external_fee, sim.log_enabled, sim.verbose = ConstantInitialLiquidity, EXT_FEE, False, False
    return sim.calculate_loss(A, FEE, prices, oracle, BANDS, 0.25)


def main(input_path, starts):
    worst = 0.0
    print(f"{'start':>11} {'A':>5} {'llamma_scan':>12} {'v2 Python':>12} {'diff':>10}")
    with tempfile.TemporaryDirectory() as tmp:
        for start in starts:
            for A in AS:
                trace = Path(tmp) / f"{start}-{A}.csv"
                done = subprocess.run([str(HERE / "build" / "llamma_scan"), input_path, "/dev/null", "--bands", str(BANDS),
                                       "--fee", str(FEE), "--ext-fee", str(EXT_FEE), "--trace", str(start), str(A),
                                       str(trace)], check=True, capture_output=True, text=True)
                ours = float(done.stderr.split("loss ")[1].split()[0])
                with trace.open(newline="") as stream:
                    theirs = v2_loss(list(csv.DictReader(stream)), A)
                worst = max(worst, abs(ours - theirs))
                print(f"{start:>11} {A:>5} {ours:>12.6f} {theirs:>12.6f} {ours - theirs:>+10.1e}")
    print(f"largest difference {worst:.1e} (in loss, a fraction of the position)")


if __name__ == "__main__":
    main(sys.argv[1], [int(s) for s in sys.argv[2:]] or STARTS)
