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
uv run python scripts/value_integral.py               # share of value over time
uv run python scripts/yb_distribution.py              # example YB allocation
uv run python scripts/plot_pnl_histogram.py --no-show --after-entry-exit   # optional chart
```

Run them in this order. `all_users_pnl.py` goes up to the latest finalized
block and only fetches what is new since its last run, which takes about a
minute. The first run, with an empty `cache/`, takes about 15 minutes.
`value_integral.py` reads the caches that run just wrote. Commit the
refreshed CSVs and PNGs together with any code change that produced them.

## Outputs

| File | Rows | Main columns |
|---|---|---|
| `pnl_per_address_usd.csv` | one per address | **`net_pnl_after_entry_exit_usd`** (the number to use), `net_pnl_usd`, `entry_exit_cost_usd`, `position_pnl_usd`, `yb_usd`, `avg_pos_usd`, `markets`, `hybrid_vault`, `as_of_block` |
| `pnl_all_users.csv` | one per (market, address) | the same in the market's asset (BTC or ETH), both PnL views, YB amounts, deposits / withdrawals, USD columns |
| `value_integral.csv` | one per address | `days` (∫ own value / all users' value dt), `fraction` (= days / window length), `markets`, `from_block`, `to_block` |
| `yb_distribution.csv` | one per address | `yb_compensation`, `yb_proportional`, `yb_total` (example amounts set at the top of the script) |
| `pnl_per_user.csv`, `pnl_histogram_*.png` | | per-user totals in BTC-equivalent and the histograms (`plot_pnl_histogram.py`) |

## How it is measured

- **Window.** PnL runs from 2025-10-15 12:02:22 UTC (`WINDOW_START` in
  `yb.py`) to the end block. v1 positions that existed then are valued at
  that moment, and YB that was claimable then is not counted. The value
  integral starts at the first YB airdrop, block 23582267 (09:42:23 UTC the
  same day).
- **Users.** All addresses holding LT or gauge shares, whether staked or
  not, except protocol contracts: gauges, LTs, the fee receiver and the
  wallets in `EXCLUDED_WALLETS`. A **HybridVault counts as its owner**: its
  positions, YB and deposits / withdrawals are added to the owner's own.
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

## Other scripts

- `scripts/debug_user.py 0xADDR MARKET` (`debug_user_pps.py` for
  `pricePerShare`): one address in one market, event timeline and integrated
  PnL, straight from the chain. Passing a vault follows its owner.
- `scripts/classify_users.py`: labels each address (EOA, Safe, other contract)
  and whether a contract can rescue tokens → `pnl_all_users_classified.csv`.
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
