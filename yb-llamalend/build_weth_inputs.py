"""Rebuild the WETH simulator inputs of the 2026-11-13 finalist runs from the Git LFS sources, offline.

The WETH-only part of curve-fx-optimization/scripts/data/rebuild_finalist_inputs.py (that script needs the BTC sources
too): the same packing, and each output is published only when its SHA-256 equals data/market/finalist-inputs.json.

Sources (git lfs pull, see setup.sh), under curve-fx-sim/curve-fx-optimization/data/market:
  pyth/ethusd/reports-20250324-20261001.npz             Pyth Pro Crypto.ETH/USD, integer seconds
  pyth/ethusd-20261109/tail-20261001-20261109.npz       the same feed to 2026-11-09 (fixed_rate@1000ms)
  primitives-d25-20250324-20261109/eth-usdc.npz         ETH in USDC per 12 s slot (Binance ETH/USDT x USDC/USDT)
  primitives-d25-20250324-20261109/crvusd.npz           crvUSD in USDC per slot and the crvUSD aggregator
Outputs:
  pyth/ethusd-20261109/reports-20250324-20261109.npz    the reports the policy is delivered
  binance/spot/blocks-20250324-d25-20261109/eth-d22-blocks-20250324-20261109.npz
                                                        block tape: price in USDC, arb_price in crvUSD, rate oracle

usage: uv run --project curve-fx-sim/curve-fx-optimization --no-sync python build_weth_inputs.py
"""

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPT = HERE / "curve-fx-sim" / "curve-fx-optimization"

spec = importlib.util.spec_from_file_location("rebuild", OPT / "scripts" / "data" / "rebuild_finalist_inputs.py")
rb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rb)


def main() -> int:
    manifest = json.loads(rb.MANIFEST.read_text())
    rel = lambda path: str(path.relative_to(rb.ROOT))  # noqa: E731
    eth_usdc, crvusd = rb.PRIM / "eth-usdc.npz", rb.PRIM / "crvusd.npz"
    for path in (rb.ETH_BASE, rb.ETH_TAIL, eth_usdc, crvusd):
        if not path.exists() or path.stat().st_size < 1000:
            raise SystemExit(f"missing source {rel(path)}: run git lfs pull (setup.sh)")
        if rb.sha256(path) != manifest["sources"][rel(path)]["sha256"]:
            raise SystemExit(f"source {rel(path)} differs from the manifest")

    base, tail = rb.load(rb.ETH_BASE), rb.load(rb.ETH_TAIL)
    assert tail["ts"][0] > base["ts"][-1]
    usdc, rate = rb.load(eth_usdc), rb.load(crvusd)
    assert len(usdc["price"]) == len(rate["q"]) and int(usdc["t0"]) == int(rate["t0"])
    outputs = {
        rb.MARKET / "pyth/ethusd-20261109/reports-20250324-20261109.npz": (
            {"ts": rb.np.concatenate([base["ts"], tail["ts"]]),
             "price": rb.np.concatenate([base["price"], tail["price"]])},
            "pyth-ethusd-whole-second-20261109"),
        rb.TAPES / "eth-d22-blocks-20250324-20261109.npz": (rb.tape(usdc, rate, None), "d22-block-tapes"),
    }
    failed = []
    for path, (arrays, dataset_id) in outputs.items():
        want = manifest["outputs"][rel(path)]["sha256"]
        if path.exists() and rb.sha256(path) == want:
            print("up to date", rel(path))
            continue
        if rb.write(path, arrays, dataset_id, want) != want:
            failed.append(rel(path))
    if failed:
        raise SystemExit("outputs differ from the manifest (not written): " + ", ".join(failed))
    print("WETH inputs match", rel(rb.MANIFEST))
    return 0


if __name__ == "__main__":
    sys.exit(main())
