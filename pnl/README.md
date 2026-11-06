# Yield Basis per-user PnL

Per-address PnL, time-weighted share of value and an example YB allocation for
every Yield Basis market (v1 0–2, v2 3–6, v3 7–10), built straight from chain
data over JSON-RPC.

## Setup

1. An Ethereum mainnet **archive** node (historical state is sampled at every
   balance change). Copy `.env.example` to `.env` and set:
   - `ETH_RPC_URL` — the node, e.g. `http://192.168.81.3:8545`
   - `ETH_RPC_URL_FALLBACK` — optional, used if the primary is unreachable
   - `ETHERSCAN_API_KEY` — only for `classify_users.py`
2. [uv](https://docs.astral.sh/uv/). Python 3.13 and all dependencies come
   from `pyproject.toml` / `uv.lock`; run everything from this directory with
   `uv run python …`.

## Refreshing the numbers

```sh
uv run python scripts/all_users_pnl.py --entry-exit   # PnL per address
uv run python scripts/classify_users.py               # finds contracts that are not users
uv run python scripts/value_integral.py               # share of value over time
uv run python scripts/value_integral_v3cap.py         # variant: v2 capped by v3, no v1
uv run python scripts/compare_value_integrals.py      # who drops out between the two
uv run python scripts/yb_allocation.py                # YB per wallet, both variants
uv run python scripts/plot_pnl_histogram.py --no-show --after-entry-exit   # optional chart
```

Run them in this order. `all_users_pnl.py` goes up to the latest finalized
block and only fetches what is new since its last run, which takes about a
minute. The first run, with an empty `cache/`, takes about 15 minutes.
`value_integral.py` reads the caches that run just wrote. If `classify_users.py`
reports new addresses, run `all_users_pnl.py` once more before going on, so
they get no share. A new contract starts out with no share. To give it one,
show on a fork that it can move YB sent to it, then move it from
`non_user_contracts.csv` to `contract_recipients.csv`. Commit the refreshed CSVs
and PNGs together with any code change that produced them.

## Outputs

| File | Rows | Main columns |
|---|---|---|
| `pnl_per_address_usd.csv` | one per address | **`net_pnl_after_entry_exit_usd`** (the number to use), `net_pnl_usd`, `entry_exit_cost_usd`, `position_pnl_usd`, `yb_usd`, `avg_pos_usd`, `markets`, `hybrid_vault`, `as_of_block` |
| `pnl_all_users.csv` | one per (market, address) | the same in the market's asset (BTC or ETH), both PnL views, YB amounts, deposits / withdrawals, USD columns |
| `value_integral.csv` | one per address | `days` (∫ own value / all users' value dt), `fraction` (= days / window length), `markets`, `from_block`, `to_block` |
| `value_integral_v3cap.csv` | one per address | as above for the variant, plus `days_v2`, `days_v3`, `v3_avg_BTC` and `v3_avg_WETH` (the caps), `v3_from_BTC` and `v3_from_WETH` (block of the first v3 deposit, where each average starts) |
| `value_integral_compare.csv` | one per address of either measurement | `status` (dropped, reduced, increased), `reason`, old and new shares, what the wallet still holds in v1 and v2, PnL |
| `yb_allocation_full.csv`, `yb_allocation_v3cap.csv` | one per wallet of that variant | `yb` (1,406,250 YB in total, in proportion to `days`, no loss adjustment), `fraction`, `days`, `markets`, `hybrid_vault` |
| `yb_allocation_difference.csv` | one per wallet of either | `yb_full`, `yb_v3cap`, `yb_difference` (v3cap − full), `status`, biggest change first |
| `non_user_contracts.csv` | one per contract | `address`, `addr_type`: contracts that get no share (see Users below); `classify_users.py` only ever adds to it |
| `contract_recipients.csv` | one per contract | `address`, `name`, `reaches_yb_by`: contracts that keep their share because they can move YB sent to them; kept by hand |
| `yb_distribution.csv` | one per address | older example with a loss compensation bucket: `yb_compensation`, `yb_proportional`, `yb_total` (`scripts/yb_distribution.py`, amounts set at the top of it) |
| `pnl_per_user.csv`, `pnl_histogram_*.png` | | per-user totals in BTC-equivalent and the histograms (`plot_pnl_histogram.py`) |

## How it is measured

- **Window.** PnL runs from 2025-10-15 12:02:22 UTC (`WINDOW_START` in
  `yb.py`) to the end block. v1 positions that existed then are valued at
  that moment, and YB that was claimable then is not counted. The value
  integral starts at the first YB airdrop, block 23582267 (09:42:23 UTC the
  same day).
- **Users.** All addresses holding LT or gauge shares, whether staked or
  not, except protocol contracts: gauges, LTs, the fee receiver and the
  wallets in `EXCLUDED_WALLETS`. An address counts if it is an EOA (EIP-7702
  ones included), a Safe, or a contract in `contract_recipients.csv`: one shown
  on a fork to be able to move YB sent to it, such as the Beefy and Giddy
  strategies and owner-run position managers. Every other contract is in
  `non_user_contracts.csv` and has no row in any output: pools, lending
  markets, zaps and other contracts that can't recover tokens, unverified
  contracts, and any HybridVault one of them owns. The old FeeDistributor and
  the DAO are there too: they could recover YB, but they are the protocol's own.
  Zaps still pass users' deposits and withdrawals through to the users behind
  them. A **HybridVault counts as its owner**: its positions, YB and deposits /
  withdrawals are added to the owner's own, so anything due to the vault goes to
  the owner.
- **PnL** compares each position with simply holding the asset: balance ×
  change in share value, summed over every interval between balance changes,
  plus YB earned (YB received, plus YB claimable at the end). v1 is valued
  at redemption (`preview_withdraw`), because v1 `pricePerShare` ran far
  above what a withdrawal paid. v2 and v3 use `pricePerShare`.
- **Entry/exit costs** (`--entry-exit`) are what a user paid above the share
  value on deposit, or got below it on withdrawal: mostly the pool fee on
  imbalanced deposits and withdrawals. With these costs included, a user who
  entered and left inside the window gets exactly withdrawn − deposited + YB.
  Deposits and withdrawals made through StakeZap or another router are
  charged to the user behind them (found from the transaction receipt).
  Flash-loan bots' same-block round trips are skipped.
- **USD.** Each gain or loss is converted at the asset's crvUSD price (from
  the market pool's `price_oracle`) at the moment it happened; YB at its
  crvUSD value when received. This is still PnL compared with holding
  BTC/ETH, just expressed in USD: the asset's own price move is not in it.
- **Value integral.** At every sampled block, each user's positions are
  valued at redemption in USD and divided by all users' total. `days` is
  that share integrated over time, so it only depends on relative value.
  Built-in checks:
  - the users' `days` must add up to the window length in days;
  - users' LT and gauge shares must equal `totalSupply` minus the excluded
    holders, on chain, at both ends of the window.

  If either check fails, the script writes no CSV.

- **v3-capped value integral.** Same window and valuation, but v1 is not
  counted, and v2 counts only for what the user carried into v3. Per user and
  asset the weight is `min(V2(t), A3) + V3(t)`, taken in the asset and then
  converted to USD. `A3` is the user's average v3 value in that asset from
  their first v3 deposit in it to the end block, counting time after that with
  nothing deposited as zero: migrating late costs nothing, leaving early lowers
  it. There are two assets for this: BTC (WBTC, cbBTC and tBTC added together
  1:1) and WETH, so moving between BTC wrappers counts as migrating. A wallet
  that never held v3 gets nothing, and a small v3 deposit gives a small v2
  allocation. `days` add up to the time with something countable, from the
  first eligible v2 deposit on.

## Other scripts

- `scripts/debug_user.py 0xADDR MARKET` (`debug_user_pps.py` for
  `pricePerShare`): one address in one market, event timeline and integrated
  PnL, straight from the chain. Passing a vault follows its owner.
- `scripts/classify_users.py`: labels each address (EOA, Safe, pool, other
  contract) and whether a contract can rescue tokens →
  `pnl_all_users_classified.csv`, and adds non-users to
  `non_user_contracts.csv`, leaving out `contract_recipients.csv`. Use
  `--block N` to match a pinned end block.
- `scripts/btc_time_integral.py`: the older BTC×blocks integral for markets
  3–6 (`btc_time_integral.csv` is a May 2026 snapshot).
- `traverse.py`: lists every market from the factory. `scripts/bench_rpc.py`:
  RPC transport benchmark.

Every script's docstring has its full options (`--help` on the main ones).

## Notes

- `cache/` (gitignored) holds fetched events, sampled state, receipts and
  block timestamps. It is safe to delete; the next run refetches it.
- On a public RPC that caps `eth_getLogs` ranges, set `LOGS_CHUNK=1000`.
- `--end-block N` pins a run to a block, e.g. to reproduce a snapshot.
