# btc-fundamental-pnl

Fundamental PnL of the Yield Basis BTC markets across v1 (0–2), v2 (3–5) and
v3 (7–9). It counts users and admin together, in BTC. The WETH markets (6 = v2
code, 10 = v3) are reported per asset only. The BTC "system" totals cover the
BTC markets alone.

## Run

```sh
cp .env.example .env               # ETH_RPC_URL = archive node, ETHERSCAN_API_KEY
uv run python fetch_events.py      # ~3 min  -> data/events.csv.xz   (flows)
uv run python fetch_states.py      # ~4 min  -> data/states.csv.xz, data/markets.json
uv run python compute_pnl.py       # ~5 min  -> data/pnl.csv.xz, data/monthly.csv
uv run python plot_pnl.py          #         -> pics/*.pdf, pics/*.png, data/weekly.csv
uv run python validate.py          # boa fork cross-check (optional)
```

`plot_pnl.py --show` opens the figures in interactive QtAgg windows.

## Method

* **Equity of the whole market (users + admin).** LT books E =
  `value_oracle / LT._price_oracle()`, which is exactly `liquidity.total +
  liquidity.admin`. `validate.py` checks this on boa forks of v2/v3 LTs, and
  checks the Python LEVAMM math against `value_oracle()` on every row.
* **"As designed"**: the LP is priced at the virtual price the pool guarantees
  to LPs. That is `(1 + xcp_profit) / 2` for twocrypto v2.1.0d (v1/v2) and
  `lp_xcp_profit` for v3.0.0. The market is valued at `price_scale` and in
  crvUSD (agg = 1).
* **PnL** is the sum of ΔE over sample intervals without a flow of that market.
  Flows are AMM `AddLiquidityRaw` / `RemoveLiquidityRaw`, and each flow block is
  isolated and dropped.
* **Relative PnL** is an equity-weighted, chain-linked return index over all BTC
  markets: r = ΣΔE / ΣE_start per interval, index = Π(1 + r). It is the growth
  of 1 BTC kept pro rata in the BTC markets of the time, v1 → v2 → v3.
* **Admin fee charged** comes from replaying `LT._calculate_values` exactly at
  every sample (it matches `pricePerShare()` on every row). The increments of
  the admin's claim, plus what `WithdrawAdminFees` converts into fee-receiver
  shares, are the admin fee. System = users + admin fee; the users' part is
  system minus admin fee.
* **Reliable line** (dashed): equals the as-designed value whenever a market's
  pool is within 2% of its oracle (`--dev-threshold`). While the pool is
  further off, the as-designed value is inflated, so that market's value is
  held at max(value at the start of the deviation, non-inflated value), with
  the non-inflated value chained from the start of the deviation. Once the
  pool is back within 2%, the line rejoins the as-designed value; losses or
  gains realised during the window show up at that point. A group's line is
  its as-designed index times the equity-weighted average of its markets'
  factors (reliable value / as-designed value). It has no path dependence,
  equals the solid line whenever every pool is balanced, and always lies
  between its markets' own lines.
* **Balanced out (non-inflated)**, in `rebalance.py`: the value if the system
  were balanced out at the current market price. `price_scale` walks to the
  market price in 40 steps. After each step, arbitrage brings the pool back
  within its fee of the market, and brings the LEVAMM back within its fee of the
  LP's real value. The fees those trades pay stay in the system, and donation
  shares are burned as `tweak_price` does. The pools use StableswapMath, so
  this runs exactly in vectorised float math (D matches `pool.D()` to ~1e-16).
  * The latent cost of balancing out = book (crvUSD) − balanced-out value.
  * A one-jump variant (`idx_jump_*`) is kept in the data only. With large
    gaps it pushes the LEVAMM out of its safe range and isn't meaningful.
* Other comparison valuations:
  * **LT books**: what LT itself uses.
  * **Mark-to-market**: real pool balances at the oracle price. The balanced-out
    value follows it closely, because in a stepwise rebalance arbitrageurs
    trade at about market prices.

## Figures

| file | content |
|---|---|
| `btc_relative_pnl` | all BTC markets: system (as designed) and reliable lines only, on pale zones showing which version held most TVL |
| `wbtc_ / cbbtc_ / tbtc_ / weth_relative_pnl` | the same per asset, chaining that asset's markets across versions |
| `btc_fundamental_pnl` | relative PnL: system vs users' part (gap = admin fee) + reliable dashed line; non-inflated (balanced-out) value; same in BTC; BTC price; weekly APR; realised vol |
| `btc_pnl_valuations` | relative PnL under every valuation; latent cost of balancing out; `price_scale` lag of each pool |
| `btc_pnl_by_market` | per asset: relative PnL and equity of each version's market |
| `btc_apr_vs_vol` | weekly APR vs realised vol, split by whether the pools tracked the market |
