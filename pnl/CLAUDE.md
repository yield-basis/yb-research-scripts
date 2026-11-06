# pnl/ — Yield Basis per-user PnL analysis

Per-user PnL and YB-token distribution analysis across Yield Basis markets
(all of them: v1 0–2, v2 3–6, v3 7–10). Pulls events + on-chain state directly via JSON-RPC,
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
2. `scripts/classify_users.py [--block N]` → `pnl_all_users_classified.csv`
   (adds `addr_type` + `has_rescue` for each address) and adds everything that
   is neither an EOA, a Safe nor in `contract_recipients.csv` to
   `non_user_contracts.csv`. If it reports new addresses, re-run step 1 before
   going on.
3. `scripts/value_integral.py` → `value_integral.csv` (each user's share of
   all users' USD value integrated over time since `AIRDROP_1_BLOCK`, in
   days, all markets; reads step 1's newest caches, run it right after).
   `scripts/value_integral_v3cap.py` → `value_integral_v3cap.csv` is the
   variant counting v2 only up to what the user holds on average in v3, and
   v1 not at all; `scripts/compare_value_integrals.py` →
   `value_integral_compare.csv` shows who drops out between the two.
   `scripts/btc_time_integral.py` → `btc_time_integral.csv` is the older
   BTC×blocks version for markets 3–6, kept as a May snapshot.
4. `scripts/yb_allocation.py` → `yb_allocation_{full,v3cap,difference}.csv`:
   `YB_TOTAL` per wallet in proportion to `days` in each integral, no loss
   adjustment. `scripts/yb_distribution.py` → `yb_distribution.csv` is the
   older example allocation (half to losers ∝ |`net_pnl_after_entry_exit_usd`| from
   `pnl_per_address_usd.csv`, half to everyone ∝ `days` from
   `value_integral.csv`; warns if their end blocks differ).
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
  `factory.fee_receiver()`, `ZERO_ADDR`, `EXCLUDED_WALLETS` and
  `NON_USER_CONTRACTS` (both in `yb.py`). When adding a new aggregation script,
  apply the same filter or totals will be inflated by non-user contracts.
- **`NON_USER_CONTRACTS`** (`non_user_contracts.csv`, kept by
  `classify_users.py`, which only adds to it). EOAs (EIP-7702 included), Safes
  and `CONTRACT_RECIPIENTS` get a share; every other contract doesn't: pools,
  lending markets, zaps, unverified contracts and HybridVaults they own, plus
  the old FeeDistributor `0xd11b…7a90` (`fee_receiver()` now returns the fee
  splitter, so it isn't caught by that check) and the DAO, which could recover
  YB but are the protocol's own. **Don't add these to
  `EXCLUDED_WALLETS`**: `all_users_pnl.py` must keep them during attribution and
  only drop their rows at the end. Skipping them there loses the zaps' routing
  (StakeZap is on the list) and thins the sampled blocks, which changes other
  users' PnL. The value integrals do leave them out of every total.
- **`CONTRACT_RECIPIENTS`** (`contract_recipients.csv`, kept by hand). Contracts
  that keep their share because they can move YB sent to them: Beefy strategies
  (harvest sells the whole YB balance into the vault), Giddy strategies, owner-run
  position managers, Stake DAO contracts. Add one only after moving YB out of it
  on a fork as its real controller; `has_rescue` alone isn't enough, because some
  sweeps refuse YB (the `Wrapped syb-*` strategies do). When a recipient owns a
  HybridVault, that vault must not be in `non_user_contracts.csv` either, or its
  position is lost.
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
- **Value integral invariant.** `value_integral.py` values every position at
  redemption in USD at each sampled block, so `days` = ∫ V_user / V_all dt
  must add up to the window length exactly (the total comes from per-market
  running totals, each user's part from its own trajectory), and users' LT /
  gauge shares must equal totalSupply minus excluded holders on chain at
  both ends. The script refuses to write the CSV otherwise. Block
  timestamps are cached in `cache/block_timestamps.pkl`.
- **v3-capped integral.** `value_integral_v3cap.py` weighs a user at
  Σ_asset [min(V2(t), A3) + V3(t)], in the asset and then USD; A3 is the
  user's average V3 from their own first v3 deposit in that asset to the end
  block (zero while not deposited after that), so a late migrator is not
  penalised and an early leaver is; a deposit just before the end block
  therefore earns a near-full cap. The cap is per user on BTC as a whole
  (WBTC, cbBTC and tBTC added 1:1) and on WETH: moving between BTC wrappers counts as migrating,
  though LTMigrator itself stays within one token. When the cap binds, the
  v2 positions scale down together and each keeps its own pool price. The
  cap is non-linear, so v2 is integrated per user over the grid instead of
  through the J index. Any v3 deposit keeps a wallet in, so drop-outs are
  exactly the wallets that never held v3. Σ days is the time with something
  countable (357 days, not the 385-day window).
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
