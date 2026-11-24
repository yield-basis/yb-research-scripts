#!/usr/bin/env bash
# LlamaLend loss / discount vs A for yb-WETH collateral and WETH borrowed, from a run's trajectory.csv.
#
#   llamma/run_scan.sh runs/weth-live-pyth-60s [--margin 0.015] [-- llamma_scan options]
#
# Writes into the run: llamma_input.bin (oracle weth.oracles-ll and redemption value per row), llamma_scan_A.csv
# (one row per A), llamma_scan_A.png and llamma_worst_A<optimal>.csv (the worst window replayed). Use a 60 s run
# (TRACE_INTERVAL=5 ./run.sh ...): the losses come from minute-scale moves that 300 s rows miss.
set -euo pipefail
cd "$(dirname "$0")"
run=$(realpath "../$1")
shift
margin=0.015  # loan_discount - liquidation_discount; curvefi uses 0.03 for volatile collateral against USD
if [[ ${1:-} == --margin ]]; then margin=$2; shift 2; fi
[[ ${1:-} == -- ]] && shift
py=(uv run --project ../curve-fx-sim/curve-fx-optimization --no-sync python)
As=10,13,17,23,31,40,53,70,93,123,163,215,284,375,496,656,866,1145,1513,2000
"${py[@]}" prepare_llamma_input.py "$run/trajectory.csv" "$run/llamma_input.bin"
./build/llamma_scan "$run/llamma_input.bin" "$run/llamma_scan_A.csv" --A "$As" --bands 4 --fee 0.002 \
  --ext-fee 0.0005 --min-minutes 30 --max-minutes 60 --stride 1 --top-frac 1e-4 "$@"
"${py[@]}" plot_llamma_scan.py "$run/llamma_scan_A.csv" "$run/llamma_input.bin" --margin "$margin"
