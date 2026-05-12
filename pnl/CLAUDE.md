# pnl/ — Yield Basis per-user PnL analysis

Per-user PnL and YB-token distribution analysis across Yield Basis BTC markets
(markets 3–6 currently). Pulls events + on-chain state directly via JSON-RPC,
caches intermediate artifacts, and emits CSVs / histograms.

## Layout

- `yb.py` — chain helpers: factory resolution via ENS (`factory.yieldbasis.eth`),
  market struct, RPC client with fallback, key constants (`AIRDROP_1_BLOCK`,
  `EXCLUDED_WALLETS`, `fee_receiver`). Everything imports from here.
- `traverse.py` — smoke test: prints every market the factory exposes.
- `scripts/` — the actual pipeline. Each file has a docstring at the top
  documenting usage; read those first rather than guessing.
- `cache/` — pickled events + PPS samples keyed by `(markets, end_block)`.
  Gitignored. Safe to delete; will be re-fetched.

## Pipeline (run order)

The scripts form a DAG, not a single entry point. The usual order:

1. `scripts/all_users_pnl.py 3 4 5 6` → `pnl_all_users.csv`
   (events + PPS samples → per-(user, market) PnL spreadsheet; writes `cache/`)
2. `scripts/classify_users.py` → `pnl_all_users_classified.csv`
   (adds `addr_type` + `has_rescue` for each address)
3. `scripts/btc_time_integral.py` → `btc_time_integral.csv`
   (per-user BTC×blocks integral since `AIRDROP_1_BLOCK`; reuses `cache/`)
4. `scripts/yb_distribution.py` → `yb_distribution.csv`
   (combines the two CSVs above into a YB-token allocation)
5. `scripts/plot_pnl_histogram.py` (`--pps` or `--redeem`) → PNG
6. `scripts/debug_user.py` / `debug_user_pps.py` — single-user drill-down,
   no cache needed.

`bench_rpc.py` is a standalone RPC-transport benchmark — not part of the
pipeline.

## Environment

`uv` project (`pyproject.toml`, `uv.lock`). Always invoke scripts via
`uv run python scripts/<name>.py …`.

Required env vars (see `.env.example`):
- `ETH_RPC_URL` — mainnet RPC; archive-capable is needed for historical PPS.
- `ETH_RPC_URL_FALLBACK` — optional, used if primary is unreachable.
- `ETHERSCAN_API_KEY` — used by `classify_users.py` for ABI lookups.

## Conventions / gotchas

- **User-set definition.** "Users" in any aggregation excludes: gauge, LT,
  `factory.fee_receiver()`, `ZERO_ADDR`, and `EXCLUDED_WALLETS` (defined in
  `yb.py`). When adding a new aggregation script, apply the same filter or
  totals will be inflated by non-user contracts.
- **`EXCLUDED_WALLETS`.** Contracts holding LT that cannot rescue ERC20s
  (no `rescueERC20` / `sweep` / etc. — see `classify_users.py`'s
  `RESCUE_SIGS`). Any YB sent to them is unrecoverable, so excluding them
  matches reality.
- **`AIRDROP_1_BLOCK = 23582267`** (2025-10-15 09:42:23 UTC) is the anchor
  for time-integrated metrics. Don't redefine locally.
- **Two PnL flavours.** `net_pnl_pps` uses `LT.pricePerShare()` (NAV,
  slippage-free); `net_pnl_redem` uses `preview_withdraw(P)` (marginal
  redemption value, includes AMM slippage at the probe size). They're not
  interchangeable — pick deliberately.
- **Multicall3.** All historical state sampling batches into a single
  Multicall3 call per block (address `0xcA11bde05977b3631167028862bE2a173976CA11`).
  When adding a new sampled metric, fold it into the existing multicall in
  `all_users_pnl.py` rather than adding a separate per-block RPC.
- **Caches are append-friendly.** `all_users_pnl.py` writes
  `events_<markets>_to_<block>.pkl` and `pps_<markets>_to_<block>.pkl`.
  Downstream scripts (e.g. `btc_time_integral.py`) read whichever file
  matches their market set and may extend the PPS cache with a single
  additional block — keep this contract intact.

## Outputs

CSVs and PNGs at the directory root are committed (treated as
research-artifact snapshots, not regenerated build output). If you
regenerate them, commit the refresh alongside the code change so the
checked-in artifact stays consistent with the script that produced it.
