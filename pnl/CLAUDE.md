# pnl/ — Yield Basis per-user PnL analysis

Per-user PnL and YB-token distribution analysis across Yield Basis markets
(all of them, v1 0–2, v2 3–6, v3 7–10, for the PnL; the YB distribution
scripts still use 3–6). Pulls events + on-chain state directly via JSON-RPC,
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

1. `scripts/all_users_pnl.py --entry-exit` (no markets = all) →
   `pnl_all_users.csv` (events + PPS samples → per-(user, market) PnL
   spreadsheet over the window from `WINDOW_START`; writes `cache/`;
   `--entry-exit` adds the entry/exit cost columns) and
   `pnl_per_address_usd.csv` (one row per address, all markets in USD, with
   the as-of block; use `net_pnl_after_entry_exit_usd`). Re-running it is the
   refresh: it goes to the finalized head and only fetches what's new (~1 min).
2. `scripts/classify_users.py` → `pnl_all_users_classified.csv`
   (adds `addr_type` + `has_rescue` for each address)
3. `scripts/btc_time_integral.py` → `btc_time_integral.csv`
   (per-user BTC×blocks integral since `AIRDROP_1_BLOCK`; reuses `cache/`)
4. `scripts/yb_distribution.py` → `yb_distribution.csv`
   (combines the two CSVs above into a YB-token allocation)
5. `scripts/plot_pnl_histogram.py [--no-show]` → `pnl_histogram_net.png` +
   `pnl_per_user.csv` (per-user distribution, summed over markets in BTC-eq;
   `--after-entry-exit` for net PnL after entry/exit costs, `--pps` /
   `--redeem` for a pure view, `--by-market` per (market, user))
6. `scripts/debug_user.py` / `debug_user_pps.py` — single-user drill-down,
   no cache needed (whole history from deploy, no entry/exit costs or pending
   YB; `net_pnl_redem` / `net_pnl_pps` of a v2/v3 row should match).

`bench_rpc.py` is a standalone RPC-transport benchmark — not part of the
pipeline.

## Environment

`uv` project (`pyproject.toml`, `uv.lock`, Python pinned to 3.13 in
`.python-version`). Always invoke scripts via `uv run python scripts/<name>.py …`.
`[tool.uv] constraint-dependencies` floors transitive packages at the
releases that fixed `uv audit` advisories (check with `uv audit --frozen`);
`web3<8` / `polars<2` keep the APIs the scripts use, and `pyunormalize<18`
keeps web3 7's ENS resolution (used for the factory) working.

Required env vars (see `.env.example`):
- `ETH_RPC_URL` — mainnet RPC; archive-capable is needed for historical PPS.
- `ETH_RPC_URL_FALLBACK` — optional, used if primary is unreachable.
- `ETHERSCAN_API_KEY` — used by `classify_users.py` for ABI lookups.

## Conventions / gotchas

- **User-set definition.** "Users" in any aggregation excludes: gauge, LT,
  `factory.fee_receiver()`, `ZERO_ADDR`, and `EXCLUDED_WALLETS` (defined in
  `yb.py`). When adding a new aggregation script, apply the same filter or
  totals will be inflated by non-user contracts.
- **HybridVaults belong to their owners.** A HybridVault (yb-core
  `contracts/HybridVault.vy`, factory `HYBRID_VAULT_FACTORY` in `yb.py`, one
  vault per owner, owner fixed at creation) holds LT / gauge shares, claims
  YB to itself and deposits / withdraws in its own name. Every script maps a
  vault to its owner via `yb.hybrid_vault_owners()` (factory `vault_to_user`
  through Multicall3) before attributing anything, so the owner's row merges
  the vault with what the owner holds directly; claimable YB is queried for
  both addresses; `hybrid_vault` column names the vault. New aggregation
  scripts must do the same. The vault's scrvUSD side is not counted.
- **`EXCLUDED_WALLETS`.** Contracts holding LT that cannot rescue ERC20s
  (no `rescueERC20` / `sweep` / etc. — see `classify_users.py`'s
  `RESCUE_SIGS`). Any YB sent to them is unrecoverable, so excluding them
  matches reality.
- **`AIRDROP_1_BLOCK = 23582267`** (2025-10-15 09:42:23 UTC) is the anchor
  for time-integrated metrics. Don't redefine locally.
- **`WINDOW_START = "2025-10-15 12:02:22"`** (UTC; state at block 23582964)
  is where per-user PnL starts. v1 markets were live before it: balances are
  rebuilt from the full transfer history and valued at that block, and YB
  claimable at that block is subtracted from YB earned (gauge claims began
  10:16 UTC that day). v2/v3 start empty inside the window.
- **Two PnL flavours.** `net_pnl_pps` uses `LT.pricePerShare()` (NAV,
  slippage-free); `net_pnl_redem` uses `preview_withdraw(P)` (marginal
  redemption value, includes AMM slippage at the probe size). They're not
  interchangeable — pick deliberately. **v1 must use redemption**: v1 LT
  books at the pool's EMA oracle, so its pricePerShare runs far above what a
  withdrawal pays when price_scale lags (1.44 vs 0.89 on 2026-02-06). `net_pnl`
  = redemption for v1, pricePerShare for v2/v3.
- **USD.** `*_usd` columns value each gain or loss at the asset's crvUSD
  price (the market pool's `price_oracle`, already sampled per block) when it
  happened: PnL between consecutive sampled blocks of any market is priced at
  the end of the step; YB at its crvUSD value when received. It is the
  native-asset PnL (vs. holding the asset) in USD, not USD PnL including the
  asset's own move. The run also prints the total with native PnL at
  end-block prices, for comparison.
- **Entry/exit costs (`--entry-exit`).** Positions are marked at the sampled
  price after every balance change, so what a user paid above the mark on
  deposit, or got below it on withdrawal (mostly the pool fee on imbalanced
  adds/removes), is only in `entry_cost` / `exit_cost` /
  `net_pnl_after_entry_exit`. With them, a user who entered and left inside
  the window gets exactly withdrawn − deposited + YB. Same-block
  mint+burn round trips are skipped: flash-loan bots that deposit, call
  `distribute_borrower_fees` and exit without a Withdraw event (LT
  topic 0x5bdcb9af is DistributeBorrowerFees, not an exit). An actual v1
  withdrawal (19.8656 cbBTC) matched its redemption mark (19.8653).
  Routers: when the owner's own LT + gauge position doesn't move in the
  transaction (StakeZap `0xe862…1ec6` mints LT and stakes it in the gauge for the
  user, or withdraws and sends the user the assets), the event goes pro rata
  to the addresses whose position did move, from the receipt
  (`cache/receipts_<markets>.pkl`). Before this, $640k of the $1.07M of costs
  sat on routers. With costs included, PnL depends only on the marks at the
  window's ends (where pricePerShare and redemption agree within ~1% for
  v2/v3), which matters because **v2 pricePerShare also overshot redemption
  in the Feb 2026 lag** (a 10.556 cbBTC withdrawal marked at 12.52).
- **Fetch speed.** `all_users_pnl.py` asks for `LOGS_CHUNK` (default 10000)
  blocks per eth_getLogs and runs 8 POSTs in parallel, also for state
  sampling; set `LOGS_CHUNK=1000` on public RPCs that cap the range. Failed
  samples (preview_withdraw reverts right after each launch) are filled
  from neighbours before computing PnL.
- **Multicall3.** All historical state sampling batches into a single
  Multicall3 call per block (address `0xcA11bde05977b3631167028862bE2a173976CA11`).
  When adding a new sampled metric, fold it into the existing multicall in
  `all_users_pnl.py` rather than adding a separate per-block RPC.
- **Caches are append-friendly.** `all_users_pnl.py` writes
  `events_<markets>_to_<block>.pkl`, `pps_…`, `flows_…_from_<start>_to_…`
  and `pending_yb_…`. With no `--end-block` it runs to the *finalized* head
  (cached logs must never reorg), loads the newest cache with the same market
  set, fetches only blocks after it, and deletes the file it supersedes.
  Downstream scripts (e.g. `btc_time_integral.py`) read whichever file
  matches their market set and may extend the PPS cache with a single
  additional block — keep this contract intact.

## Outputs

CSVs and PNGs at the directory root are committed (treated as
research-artifact snapshots, not regenerated build output). If you
regenerate them, commit the refresh alongside the code change so the
checked-in artifact stays consistent with the script that produced it.
