# Liquidity response to incentives — PYUSD/crvUSD pool

Pool: `0x625E92624Bc2D88619ACCc1788365A69767f6200` (PYUSD/crvUSD stableswap).
Liquidity is measured in USD as `balances(0)/1e6 + balances(1)/1e18`, sampled at
1000 time-spaced points over 2025-10-01 … 2026-06-17 (see
`fetch_pool_liquidity.py`).

## Method

Two regions were fit independently to the model

    y = a · exp(-x / Texp) + b

with `x` in days since each region's start, `y` in $M. `a` is sign-free, `b`
(the asymptote liquidity approaches) is bounded ≥ 0, and `Texp` (the e-folding
time constant, our target) > 0. Fitting is `scipy.optimize.curve_fit`
(`fit_pool_liquidity.py`).

## Results

| Region | Window | **Texp (days)** | a ($M) | b ($M) | R² |
|--------|--------|-----------------|--------|--------|-----|
| rise (saturation) | 2026-02-01 … 2026-03-02 | **11.40 ± 0.62** | −65.52 ± 1.12 | 69.33 ± 1.27 | 0.974 |
| drop (decay)      | 2026-05-03 … 2026-06-10 | **5.92 ± 0.15**  | 42.88 ± 0.60  | 1.42 ± 0.18   | 0.981 |

![fits](pics/pool_liquidity_fit.png)

## Interpretation

- **Liquidity arrives ~2× slower than it leaves.** The incentive-driven ramp-up
  has a ~11.4-day e-folding time; the wind-down decays with a ~5.9-day time
  constant.
- The asymptotes are sensible: the rise approaches a ~$69M plateau, and the drop
  bottoms out near ~$1.4M (pool essentially emptied).
- Both fits are tight (R² ≈ 0.97–0.98), so a single exponential captures each
  region well; no second time scale is needed at this resolution.

## Reproduce

```sh
uv run python fetch_pool_liquidity.py            # -> pool_liquidity.csv.xz
uv run python fit_pool_liquidity.py              # prints params, shows overlay
uv run python fit_pool_liquidity.py --save pics/pool_liquidity_fit.png
```

Region windows are defined in `REGIONS` at the top of `fit_pool_liquidity.py`.
