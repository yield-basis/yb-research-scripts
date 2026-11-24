# yb-llamalend

How a yb-WETH (LT) position behaves as LlamaLend collateral, starting from a historical simulation of the yb-WETH pool
under the policy that is live now: Curve ownership vote 1525 attached a Pyth-driven `YBOraclizedPolicy` to the pool,
and the YieldBasis rate/fee vote gated on it set the LevAMM rate and the releverage fee.

The simulator is curve-fx-sim: [twocrypto-cpp](https://github.com/curvefi/twocrypto-cpp) (the pool and the
`report_lean` policy that `YBOraclizedPolicy` ports line by line), [fx-arb-harness](https://github.com/curvefi/fx-arb-harness)
(market events, the arbitrageur, the LevAMM actor `active_2l`, the Pyth keeper) and
[fx-optimization](https://github.com/curvefi/fx-optimization) (`fxopt`: runs, replays, data). One trajectory takes
about a second; the full-trace replay used for the figures takes about a minute.

## Setup and run

```sh
./setup.sh                          # clone the pinned commits into curve-fx-sim/, build, fetch and rebuild WETH inputs
./run.sh sim/weth-live-pyth.toml    # -> runs/weth-live-pyth/
```

`setup.sh` pins the commits of the 2026-11-13 finalist runs that the deployed policy was tuned on (twocrypto-cpp
`9680f62`, fx-arb-harness `cd02298`, fx-optimization `e053e99`). It fetches about 0.5 GB from Git LFS, and
`build_weth_inputs.py` checks both rebuilt inputs byte for byte against the SHA-256s of the finalist runs. The workspace
(2.5 GB) and the full traces are git-ignored.

## Live parameters (read on chain at block 26487413, 2026-11-24)

| | |
| --- | --- |
| pool | `0x656341Ef90b622c6634e0573772FfB7f3669b9f3`, A = 5, ma_time 600 s half-life, RPF 43%, admin fee 0, donation share cap 10% |
| policy | `YBOraclizedPolicy` `0xC4561F25034Bc70CED5f034E863dD150f2bA763a`, Pyth Pro Crypto.ETH/USD (feed 2), fixed_rate@1000ms |
| fee | base 60 bp, capture 0.80, fallback 200 bp, relax 90 s |
| price_scale driver | EMA half-lives 5400 / 10800 s, trend gate 75 bp, step cap 50 bp, report lifetime 5400 s |
| LevAMM `0x5f8D…233c` | rate 570776255 / s = 1.80% on the debt (pool donation APY 0.90%), releverage fee 140 bp |

## Inputs (2025-03-24 .. 2026-11-09)

- Block tape, one row per 12 s slot: Binance ETH/USDT × USDC/USDT (the mark); the same price in crvUSD (`arb_price`,
  over the Curve crvUSD/USDC rate), at which the arbitrageur and the LevAMM actor trade; the crvUSD aggregator as a rate
  oracle, which the fees read.
- Pyth Pro ETH/USD reports, one per second. A keeper delivers one once the stored report is an hour old, and
  arbitrageurs bring the best report published in [event − 12 s, event + 3 s].

## Outputs (`runs/<name>/`)

- `trajectory.png`: ETH and price_scale; the LT in WETH (redemption value and both YB oracles); oracle vs redemption;
  price_scale and price_oracle vs the market; the LevAMM debt to collateral ratio.
- `trajectory.csv`: one row per trace row (300 s; `TRACE_INTERVAL=5 ./run.sh ...` for 60 s). Columns:

  | column | |
  | --- | --- |
  | `eth_crvusd` | market: Binance ETH in crvUSD (the tape's `arb_price`) |
  | `price_scale`, `price_oracle` | the pool's, `price_oracle` projected to the row time as the pool's view does |
  | `oracle_weth` | `weth.oracles.yieldbasis.eth`, WETH per LT |
  | `oracle_ll_weth` | `weth.oracles-ll.yieldbasis.eth` (EMA 866 s on the fundamental, a checkpoint per row) |
  | `redemption_weth` | `emergency_withdraw` of one LT valued at the market: `redeem_weth + redeem_crvusd / eth_crvusd` |
  | `redeem_weth`, `redeem_crvusd` | its constituents: WETH, and crvUSD net of the debt share (signed) |
  | `pps_weth` | `LT.pricePerShare`, the LT's value at price_scale |
  | `staked_fraction`, `tradable` | LT staked in the gauge / supply; LevAMM x0 solvable (else both oracles use balances) |
  | `levamm_dtv`, `pool_fee`, `crvusd_aggregator` | LevAMM debt / collateral at the LP oracle; pool fee; crvUSD in USD |

- `shiftclick.png`: fxopt's standard replay figure (LP and YB APYs, the LevAMM balance sheet, pool fee and skew).
- `run.json`, `results.npz`: the run and its summary metrics.

## LT prices in WETH (`yb_prices.py`)

Float64 ports of yb-core's contracts, vectorized over trace rows:

- **`weth.oracles.yieldbasis.eth`** resolves to a `YBPriceProxy` clone that forwards `price()` to
  `YBLendingOracle.price_in_asset(LT)`. That function prices the LP at the pool's `price_oracle`, using the stableswap
  portfolio value at `price_oracle / price_scale` (curve_std `lp_oracle_2`). It values the LevAMM at
  `x0 · (2/3 · (r/√e + √e) − 1)`, where r is the LP's market price over the AMM's price oracle and e is where
  arbitrage leaves the AMM within its safe-debt limits. It then takes one LT's share after the admin fee and divides
  by `price_oracle · aggregator`.
- **`weth.oracles-ll.yieldbasis.eth`** (`YBLendingOracleLL`) is the same price with an EMA (866 s) on the
  fundamental, the LT rate at price_scale. In the simulation it is checkpointed (`price_w`) at every row.
- **Redemption value**: `LT.emergency_withdraw` takes `frac = total / (total + admin⁺) / supply` of the LevAMM's LP and
  debt, then removes the LP from the pool in balance. That yields WETH plus crvUSD net of the debt, and the crvUSD is
  converted to WETH at the market price.
- **LT accounting**: both prices share LT's per-share factor. The harness has no LT, so `LT._calculate_values` runs at
  every row on the LevAMM value at price_scale. It starts from a fresh LT (1 WETH per LT) with the live split:
  `min_admin_fee` 10% and 44.3% of LT staked. There are no deposits, withdrawals or new staking. Stakers only recover
  losses, and their profit share goes to the other holders through burned staked LT. So an unstaked LT grows faster
  than the LevAMM's equity, and the staked share falls (44.3% → 40.3%). Change the split with
  `plot_trajectory.py --min-admin-fee X --staked-fraction Y`.

`check_oracle_onchain.py` reads the deployed contracts at several blocks and recomputes all four quantities with these
ports: both oracles' `price()`, `LT.pricePerShare()` and `LT.preview_emergency_withdraw(1 LT)`. The worst relative
difference is 1.2e-12 (float64 vs uint256), checked at blocks 26337560 .. 26487560 on 2026-11-24:

```sh
ETH_RPC_URL=http://<node>:8545 uv run python yb-llamalend/check_oracle_onchain.py [block ...]
```

At those blocks the redemption value read 0.06–0.34% above `weth.oracles.yieldbasis.eth` (valued at `price_oracle`).
The LL oracle read 1.04464 at every block: nobody has called `price_w()` since 2026-10-30, so its EMA holds that
checkpoint's fundamental.

## First result (`sim/weth-live-pyth.toml`)

| | |
| --- | --- |
| LT in WETH, redemption value | 1.000 → 1.1675 (9.96% a year for an unstaked LT) |
| oracle / redemption − 1 | median −0.01%, 1–99% −0.37% .. +0.10%, min −2.1%, max +5.2%; beyond ±1% 0.06% of the time |
| LL oracle / redemption − 1 | median −0.01%, 1–99% −0.38% .. +0.09%, min −1.7%, max +5.2% |
| harness YB APY (LevAMM equity at the LP oracle, crvUSD at par) | 8.90%; LP net APY 9.0% |
| worst 7-day mean price_scale gap | 8.6% |
| LevAMM debt / collateral | 0.476 .. 0.524 (0.5 at 2x), 11,360 releverage fills |

The oracles stay close to the redemption value even when price_scale is far off, because they read the pool at
`price_oracle`. After the 2025-10-10 crash price_scale stood at 4169–4253 for hours while ETH traded around
3460–3830; LT's own valuation at price_scale was up to 61% above market value. The largest oracle gaps come from the
pool's 10-minute `price_oracle` EMA lagging a crash. At 2025-10-10 21:20 ETH was at 3458 and `price_oracle` at 3813,
so the oracle read +5.2% above the redemption value, which dipped to 0.983 WETH. The opposite case is an
under-levered LevAMM (2026-02-06, debt/collateral 0.484). There the oracle values it after arbitrage back to target,
which costs LT holders, while the redemption pays out its actual balances (−2.1%). The mid-August 2026 rally (+16% in a
day; Binance and Pyth agree) left price_scale below the market for about three weeks.

Caveats: `active_2l` is the harness's YieldBasis model, not an on-chain parity claim: no deposits or withdrawals and
no LevAMM self-touch (`yb_self_touch = false`). It starts fresh on 2025-03-24 at a $57.5M pool. The LT split above is
applied after the run. Redemption is `emergency_withdraw`; `LT.withdraw` instead swaps the crvUSD out of the pool to
repay the debt, paying the pool fee on that imbalance.

## LlamaLend: borrowing WETH against yb-WETH (`llamma/`)

A loss and liquidation-discount scan over A, as in curvefi/llamma-simulator(_v2), on phil-svg/curve's byte-exact C++
port of the LLAMMA v2 AMM (`cpp-implementations/llamalend/llv2`, pinned at `aa28f65`).

```sh
TRACE_INTERVAL=5 ./run.sh sim/weth-live-pyth.toml weth-live-pyth-60s   # 60 s rows (~8 GB peak while tracing)
llamma/run_scan.sh runs/weth-live-pyth-60s                             # ~12 min on 8 cores
```

- **Prices**: the LLAMMA's `price_oracle_contract` is `weth.oracles-ll.yieldbasis.eth`, checkpointed every row (the
  keeper, 60 s). Arbitrageurs trade at the LT's redemption value, with an external cost of 5 bp. Both are in WETH per
  LT, from `trajectory.csv` of a 60 s run. At 300 s the worst losses come out about 10× smaller, because the losses
  come from minute-scale dips.
- **Method** (`llamma_scan.cpp`): every minute starts a window lasting a random 30–60 minutes (fixed seed). Each window
  deploys a fresh LLAMMA with `BASE_PRICE = p0 · (A/(A−1) + 1e-4)` and deposits 1 LT into bands 1..4, so the oracle sits
  at the top of the position. On each row the AMM reads the oracle and one arbitrageur trades it toward the market,
  stopping at the market price net of the external fee and of the fee of the band it ends in (v2's
  `find_target_price`). The trade size comes from a port of `AMM.get_amount_for_price`; the swap itself is the engine's
  `calc_swap_out` / `apply_token_exchange`. Loss is `1 − x_down(end)/x_down(start)` and discount is
  `1 − x_down(end)/p0`. Fee 0.2% plus the v2 AMM's dynamic fees; the borrow rate is 0 within a window. Loss is the mean
  over the worst 0.01% of windows (86 of 856,748), the same tail as curvefi's worst 50 of 500k; the single worst window
  is plotted too.
- **Validation**: the engine's initial recovery `x_down(start)/p0` equals v2's `initial_recovery_coefficient(A, 4)` to
  3e-9. `check_vs_simulator_v2.py` replays 16 window × A cases through v2's Python `LendingAMM`: the losses agree to
  about 1e-7, and to at most 2.7e-4 in the 2025-10-10 crash window at A = 1000, where v2's float model and the uint256
  engine part ways.
- **Parameters** (`plot_llamma_scan.py`): optimal A is the minimum of the discount curve; `liquidation_discount` is
  the loss there; `loan_discount = liquidation_discount + margin`; `LTV_max = 1 − (discount + margin)`. The margin
  is 1.5% by default (`--margin`): WETH is borrowed against yb-WETH, a stable pair, where curvefi's 3% is meant for
  volatile collateral against USD.

Result (`runs/weth-live-pyth-60s/llamma_scan_A.png`):

| A | loss (worst 0.01%) | discount | liquidation_discount | loan_discount (+1.5%) | LTV_max (+1.5%) | LTV_max (+3%) |
| --- | --- | --- | --- | --- | --- | --- |
| 163, low end of the plateau (chosen) | 1.19% | 2.38% | 1.19% | 2.69% | 96.1% (25.7x) | 94.6% |
| 2000, argmin | 2.21% | 2.30% | 2.21% | 3.71% | 96.2% | 94.7% |
| 93, minimum for the single worst window | 1.76% | 3.85% | 1.76% | 3.26% | 94.65% | 93.2% |

The discount falls from 18.5% at A = 10 to 2.4% at A ≈ 160, then stays within 2.30–2.38% up to A = 2000. Above
A ≈ 160 the four bands span less than the worst oracle dip, so the position converts completely and the loss is that
dip's round trip whatever A is. Across the plateau the LTV changes by 0.1% while the liquidation discount nearly
doubles. The plot therefore picks the smallest A within 0.1% of the minimum discount; `--plateau-tol 0` gives the
argmin.

Every one of the worst windows is the 2025-10-10 crash, starting between 21:01 and 21:20 UTC. In that hour the
redemption value dipped to 0.983 WETH while the oracle slid from 1.044 to 1.022. At A = 163 the position converted to
WETH by 21:34, and arbitrageurs sold the LT back into it at 1.03–1.05 as the oracle recovered.

Caveats: the tail is a single event in 1.63 years of history. Arbitrageurs act once a minute and exit at the
redemption value for 5 bp; under stress, `LT.withdraw` pays the pool fee on its imbalance, up to the 2% fallback. The
data is not time-reversed, since the oracle would no longer be causal, and not volatility-rescaled (v2's stress
option). The margin is a policy choice, larger than the simulated discount itself: the oracle (LT in WETH) fell more
than 1.5% below its previous high in 11 episodes (0.21% of the time) and never more than 3% (deepest 2.96%), so a loan
opened at the maximum LTV right at a high could have become liquidatable in those episodes with 1.5%, never with 3%.
See [REPORT.md](REPORT.md) for the findings.
