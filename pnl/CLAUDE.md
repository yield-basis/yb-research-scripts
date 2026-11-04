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

1. `scripts/all_users_pnl.py` (no args = all markets) → `pnl_all_users.csv`
   (events + PPS samples → per-(user, market) PnL spreadsheet over the window
   from `WINDOW_START`; writes `cache/`)
2. `scripts/classify_users.py` → `pnl_all_users_classified.csv`
   (adds `addr_type` + `has_rescue` for each address)
3. `scripts/btc_time_integral.py` → `btc_time_integral.csv`
   (per-user BTC×blocks integral since `AIRDROP_1_BLOCK`; reuses `cache/`)
4. `scripts/yb_distribution.py` → `yb_distribution.csv`
   (combines the two CSVs above into a YB-token allocation)
5. `scripts/plot_pnl_histogram.py [--no-show]` → `pnl_histogram_net.png` +
   `pnl_per_user.csv` (per-user distribution, summed over markets in BTC-eq;
   `--pps` / `--redeem` for a pure view, `--by-market` per (market, user))
6. `scripts/debug_user.py` / `debug_user_pps.py` — single-user drill-down,
   no cache needed.

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
- **What the per-user PnL leaves out.** Positions are marked at the sampled
  price after every balance change, so a user's own deposit/withdraw
  conversion (assets paid vs the mark right after) isn't counted, nor is
  same-block deposit→withdraw cycling (bots). Checked 2026-11-04 against the
  LT Deposit/Withdraw amounts: ≤1 BTC per v1 market over the whole window;
  an actual v1 withdrawal (19.8656 cbBTC) matched the mark (19.8653).
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
  `events_<markets>_to_<block>.pkl` and `pps_<markets>_to_<block>.pkl`.
  Downstream scripts (e.g. `btc_time_integral.py`) read whichever file
  matches their market set and may extend the PPS cache with a single
  additional block — keep this contract intact.

## Outputs

CSVs and PNGs at the directory root are committed (treated as
research-artifact snapshots, not regenerated build output). If you
regenerate them, commit the refresh alongside the code change so the
checked-in artifact stays consistent with the script that produced it.
