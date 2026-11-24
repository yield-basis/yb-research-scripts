#!/usr/bin/env bash
# Build the curve-fx-sim workspace under yb-llamalend/curve-fx-sim (git-ignored) and the WETH inputs.
#
#   ./setup.sh                  # clone (or check out) the pinned commits, build, fetch and rebuild the WETH inputs
#   GIT_BASE=https://github.com/curvefi ./setup.sh     # when SSH to GitHub hangs
#
# The commits are those of the 2026-11-13 finalist runs that YBOraclizedPolicy (Curve vote 1525) was tuned on.
# Needs: C++17, CMake, Ninja, Boost (JSON), MiniZip, git-lfs, uv.
set -euo pipefail
cd "$(dirname "$0")"
base=${GIT_BASE:-git@github.com:curvefi}
declare -A repo=([twocrypto-cpp]=twocrypto-cpp [curve-fx-arb-harness]=fx-arb-harness [curve-fx-optimization]=fx-optimization)
declare -A pin=([twocrypto-cpp]=9680f62 [curve-fx-arb-harness]=cd02298 [curve-fx-optimization]=e053e99)
mkdir -p curve-fx-sim
for dir in twocrypto-cpp curve-fx-arb-harness curve-fx-optimization; do
  if [[ ! -d curve-fx-sim/$dir/.git ]]; then
    GIT_LFS_SKIP_SMUDGE=1 git clone -q "$base/${repo[$dir]}.git" "curve-fx-sim/$dir"
  fi
  git -C "curve-fx-sim/$dir" -c advice.detachedHead=false checkout -q "${pin[$dir]}" 2>/dev/null || {
    git -C "curve-fx-sim/$dir" fetch -q origin
    git -C "curve-fx-sim/$dir" -c advice.detachedHead=false checkout -q "${pin[$dir]}"
  }
done

tc=$PWD/curve-fx-sim/twocrypto-cpp
cmake -S "$tc" -B "$tc/build/release" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$tc/_install" \
  -DTWOCRYPTO_POOL_BUILD_TESTS=OFF -DTWOCRYPTO_POOL_BUILD_BENCHMARKS=OFF >/dev/null
cmake --build "$tc/build/release" --target install >/dev/null

harness=curve-fx-sim/curve-fx-arb-harness
cmake -S "$harness" -B "$harness/build/report-lean" -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCURVE_FX_NATIVE_TUNING=OFF -DCURVE_FX_ENABLE_IPO=OFF -DCMAKE_PREFIX_PATH="$tc/_install" \
  -DPOLICY_ID=report_lean -DPOLICY_HEADER_PATH="$tc/include/pools/twocrypto_fx/policies/report_lean.hpp" >/dev/null
cmake --build "$harness/build/report-lean" --target arb_evaluator_f64 --parallel >/dev/null
echo "evaluator: $harness/build/report-lean/arb_evaluator_f64"

opt=curve-fx-sim/curve-fx-optimization
(cd "$opt" && uv sync --frozen --group dev -q)
# WETH sources only (~0.5 GB): Pyth ETH/USD to 2026-10-01 plus its tail to 2026-11-09, ETH in USDC and crvUSD per slot
git -C "$opt" lfs pull --include="data/market/pyth/ethusd/reports-20250324-20261001.npz,data/market/pyth/ethusd-20261109/tail-20261001-20261109.npz,data/market/primitives-d25-20250324-20261109/eth-usdc.npz,data/market/primitives-d25-20250324-20261109/crvusd.npz"
uv run --project "$opt" --no-sync python build_weth_inputs.py

# LlamaLend: phil-svg's byte-exact LLAMMA v2 engine (sparse) and curvefi/llamma-simulator_v2, the reference model
# llamma/check_vs_simulator_v2.py compares against; then the scan driver.
mkdir -p external
if [[ ! -d external/phil-curve/.git ]]; then
  git clone -q --filter=blob:none --sparse https://github.com/phil-svg/curve.git external/phil-curve
  git -C external/phil-curve sparse-checkout set cpp-implementations/llamalend
fi
git -C external/phil-curve -c advice.detachedHead=false checkout -q aa28f65 2>/dev/null || {
  git -C external/phil-curve fetch -q origin
  git -C external/phil-curve -c advice.detachedHead=false checkout -q aa28f65
}
if [[ ! -d external/llamma-simulator_v2/.git ]]; then
  git clone -q "$base/llamma-simulator_v2.git" external/llamma-simulator_v2
fi
git -C external/llamma-simulator_v2 -c advice.detachedHead=false checkout -q 0bb370f
llv2=external/phil-curve/cpp-implementations/llamalend/llv2/src
mkdir -p llamma/build
g++ -std=c++17 -O2 -I "$llv2" llamma/llamma_scan.cpp "$llv2/llama_amm.cpp" -pthread -o llamma/build/llamma_scan
echo "LLAMMA scan: llamma/build/llamma_scan"
