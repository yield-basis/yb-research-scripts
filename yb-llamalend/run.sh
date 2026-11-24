#!/usr/bin/env bash
# One trajectory: run CONFIG's single candidate, replay it with a full trace and plot it.
#
#   ./run.sh sim/weth-live-pyth.toml [output name]     -> runs/<name>/{run.json,results.npz,trace/,shiftclick.png,
#                                                         trajectory.png,trajectory.csv}
#   TRACE_INTERVAL=5 ./run.sh ...                      # one trace row per 5 blocks (60 s) instead of 25 (300 s);
#                                                         the trace JSON grows to ~1.3 GB
set -euo pipefail
cd "$(dirname "$0")"
config=$1
name=${2:-$(basename "$config" .toml)}
out=runs/$name
fx=(uv run --project curve-fx-sim/curve-fx-optimization --no-sync)
export MPLBACKEND=Agg  # files only; the auto-selected GTK4Cairo backend needs pycairo
if [[ -e $out ]]; then
  echo "$out already exists; pass another output name" >&2
  exit 1
fi
"${fx[@]}" fxopt run "$config" --output "$out"
# one row per TRACE_INTERVAL blocks (events), 25 = 300 s
"${fx[@]}" fxopt shiftclick "$out" --ordinal 0 --trace-interval "${TRACE_INTERVAL:-25}" --output "$out/trace" >/dev/null
"${fx[@]}" python -c "from fxopt.shiftclick import save_shiftclick_plot as s; print('wrote', s('$out/trace/shiftclick.json', '$out/shiftclick.png'))"
"${fx[@]}" python plot_trajectory.py "$out/trace"
