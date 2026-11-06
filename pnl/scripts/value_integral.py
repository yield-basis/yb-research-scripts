"""Each user's share of all users' value in Yield Basis, integrated over time.

    days(user) = ∫ V_user(t) / V_all(t) dt        (t in days)

V is the redemption value (LT.preview_withdraw, gauge shares through
convertToAssets) of the user's unstaked LT plus staked gauge shares, in USD
(crvUSD at each market pool's price_oracle) at that moment, summed over all
markets, so BTC and ETH positions add up in one unit. Only relative value
matters: `days` is how many days of owning everything the user's share adds
up to, and `fraction` = days / window length.

Invariant: at every moment the shares of all users add up to 1, so Σ days is
exactly the window length in days. The total V_all is built from per-market
running totals of all balance changes, while each user's integral comes from
that user's own balance trajectory, so the sum only comes out right if every
trajectory is (no lost, duplicated or misplaced events). The users' totals of
LT and gauge shares are also reconciled with the chain (totalSupply minus the
excluded holders) at both ends of the window.

Time: block timestamps (cached in cache/block_timestamps.pkl). The value at
each sampled block (every balance change of any market, from
all_users_pnl.py's caches) holds until the next one.

Users, as in all_users_pnl.py: excludes gauges, LTs, fee_receiver, 0x0,
EXCLUDED_WALLETS and NON_USER_CONTRACTS; a HybridVault counts as its owner.

Run all_users_pnl.py first (same markets): this reads its newest event and
sample caches, and adds the start block's sample to them if missing.

Usage:
    uv run python scripts/value_integral.py [MARKET_IDX ...] [--start "2025-10-15 09:42:23"]
                                            [--end-block N] [--out FILE]
    # no markets = all; default start AIRDROP_1_BLOCK; default end = the newest
    # cache; output value_integral.csv
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import polars as pl
import requests
from tqdm import tqdm
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import all_users_pnl as A  # noqa: E402
from yb import (  # noqa: E402
    AIRDROP_1_BLOCK,
    EXCLUDED_WALLETS,
    NON_USER_CONTRACTS,
    all_markets,
    block_at,
    fee_receiver,
    hybrid_vault_owners,
    market_deploy_block,
    parse_utc,
    rpc_url,
    w3,
)

TS_CACHE = os.path.join(A.CACHE_DIR, "block_timestamps.pkl")
ERC20_ABI = A.ERC20_ABI
SUPPLY_ABI = [
    {"name": "totalSupply", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
]


def block_timestamps(blocks) -> dict[int, int]:
    """{block: timestamp}, cached (blocks never change once final)."""
    ts: dict[int, int] = A._load_pickle(TS_CACHE) or {}
    need = sorted(set(blocks) - ts.keys())
    if need:
        url = rpc_url()

        def _fetch(batch):
            payload = [{"jsonrpc": "2.0", "id": j, "method": "eth_getBlockByNumber",
                        "params": [hex(b), False]} for j, b in enumerate(batch)]
            resp = A._retry(lambda: requests.post(url, json=payload, timeout=120),
                            label=f"headers {batch[0]}..")
            resp.raise_for_status()
            by_id = {r["id"]: r["result"] for r in resp.json()}
            return {b: int(by_id[j]["timestamp"], 16) for j, b in enumerate(batch)}

        batches = [need[i:i + 100] for i in range(0, len(need), 100)]
        with ThreadPoolExecutor(A.WORKERS) as ex:
            for part in tqdm(ex.map(_fetch, batches), total=len(batches),
                             desc="block timestamps", unit="batch", leave=False):
                ts.update(part)
        A._save_pickle(TS_CACHE, ts)
        A._log(f"fetched {len(need)} block timestamps ({len(ts)} cached)")
    return {b: ts[b] for b in blocks}


def sample_block(b: int, ctx: dict) -> dict:
    """One block's sample in all_users_pnl.py's cache schema."""
    client = w3()
    mc3 = client.eth.contract(address=A.MULTICALL3, abi=A.MULTICALL3_ABI)
    any_c = next(iter(ctx.values()))
    lt = client.eth.contract(address=any_c["lt"], abi=A.LT_ABI)
    gauge = client.eth.contract(address=any_c["gauge"], abi=A.GAUGE_ABI)
    pool = client.eth.contract(address=any_c["pool"], abi=A.POOL_ABI)
    cds = (lt.functions.pricePerShare()._encode_transaction_data(),
           lt.functions.preview_withdraw(A.PROBE_LT)._encode_transaction_data(),
           gauge.functions.convertToAssets(A.PROBE_LT)._encode_transaction_data(),
           pool.functions.price_oracle()._encode_transaction_data())
    calls = []
    for c in ctx.values():
        calls += [(c["lt"], True, cds[0]), (c["lt"], True, cds[1]),
                  (c["gauge"], True, cds[2]), (c["pool"], True, cds[3])]
    calls.append((A.YB_POOL, True, cds[3]))
    rets = mc3.functions.aggregate3(calls).call(block_identifier=b)
    val = [int.from_bytes(r, "big") if ok and len(r) == 32 else 0 for ok, r in rets]
    out = {idx: dict(zip(("pps", "pw", "cta", "btc"), val[4 * j:4 * j + 4]))
           for j, idx in enumerate(ctx)}
    out["yb"] = val[-1]
    return out


def chain_totals(b: int, ctx: dict, excluded: dict[int, set[str]]) -> dict[int, tuple[int, int]]:
    """{market: (LT, gauge shares)} held by users at block b per the chain:
    totalSupply minus every excluded holder's balance."""
    client = w3()
    mc3 = client.eth.contract(address=A.MULTICALL3, abi=A.MULTICALL3_ABI)
    tok = client.eth.contract(address=A.MULTICALL3, abi=SUPPLY_ABI)  # encoder only
    supply_cd = tok.functions.totalSupply()._encode_transaction_data()
    calls, layout = [], []
    for idx, c in ctx.items():
        for t in (c["lt"], c["gauge"]):
            calls.append((t, True, supply_cd))
            for x in sorted(excluded[idx]):
                calls.append((t, True, tok.functions.balanceOf(
                    Web3.to_checksum_address(x))._encode_transaction_data()))
        layout.append((idx, len(excluded[idx])))
    rets = mc3.functions.aggregate3(calls).call(block_identifier=b)
    val = [int.from_bytes(r, "big") if ok and len(r) == 32 else 0 for ok, r in rets]
    out, i = {}, 0
    for idx, n in layout:
        lt_users = val[i] - sum(val[i + 1:i + 1 + n])
        i += 1 + n
        g_users = val[i] - sum(val[i + 1:i + 1 + n])
        i += 1 + n
        out[idx] = (lt_users, g_users)
    return out


def load_window(market_args: list[int], start: str | None, end_block: int | None):
    """Everything both integrals need, from all_users_pnl.py's caches: the window,
    per-market prices and share values on the grid of sampled blocks, every
    user's balance changes, and all users' running totals."""
    client = w3()
    all_mks = {m.idx: m for m in all_markets()}
    markets = market_args or sorted(all_mks)
    tag = "_".join(map(str, markets))
    start_block = block_at(parse_utc(start)) if start else AIRDROP_1_BLOCK
    rerun = " ".join(map(str, market_args))

    found = A._latest_cache(f"events_{tag}", end_block or 10**12)
    if not found or (end_block and found[1] != end_block):
        raise SystemExit(f"No cache/events_{tag}_to_{end_block or '<N>'}.pkl: "
                         f"run all_users_pnl.py {rerun} first")
    events_path, end_block = found
    pps_path = os.path.join(A.CACHE_DIR, f"pps_{tag}_to_{end_block}.pkl")
    if not os.path.exists(pps_path):
        raise SystemExit(f"No {pps_path}: run all_users_pnl.py {rerun} first")
    market_events = A._load_pickle(events_path)
    cache = A._load_pickle(pps_path)
    A._log(f"events and {len(cache)} samples to block {end_block} from cache/")

    ctx = {}
    for idx in markets:
        m = all_mks[idx]
        token = client.eth.contract(address=m.asset_token, abi=ERC20_ABI)
        ctx[idx] = {
            "lt": Web3.to_checksum_address(m.lt),
            "gauge": Web3.to_checksum_address(m.staker),
            "pool": Web3.to_checksum_address(m.cryptopool),
            "asset": m.asset_token.lower(),
            "sym": token.functions.symbol().call(),
            "scale": 10 ** token.functions.decimals().call(),
            "deploy": market_deploy_block(idx),
        }

    if start_block not in cache:
        cache[start_block] = sample_block(start_block, ctx)
        A._save_pickle(pps_path, cache)
        A._log(f"sampled start block {start_block} into {pps_path}")
    grid = sorted(b for b in cache if start_block <= b <= end_block)
    assert grid[0] == start_block and grid[-1] == end_block, "cache does not cover the window"
    A.fill_failed_samples(cache, sorted(cache), {i: c["deploy"] for i, c in ctx.items()})
    ts = block_timestamps(grid)
    days_total = (ts[end_block] - ts[start_block]) / 86400
    print(f"window: block {start_block} .. {end_block} = {days_total:.6f} days, "
          f"{len(grid)} sampled blocks, markets {markets}")

    # Users and their balance changes per market (as in all_users_pnl.py)
    fee_dist = fee_receiver().lower()
    seen = {A.topic_to_addr(lg["topics"][k].hex())
            for lt_t, g_t, _ in market_events.values() for lg in (*lt_t, *g_t) for k in (1, 2)}
    vault_owner = hybrid_vault_owners(seen, end_block)
    vaults_of: dict[str, list[str]] = defaultdict(list)
    for v, o in vault_owner.items():
        vaults_of[o].append(v)
    excluded: dict[int, set[str]] = {}
    deltas: dict[int, dict[str, list]] = {}
    for idx, c in ctx.items():
        excl = {A.ZERO_ADDR, c["gauge"].lower(), c["lt"].lower(), fee_dist, *EXCLUDED_WALLETS,
                *NON_USER_CONTRACTS}
        excluded[idx] = excl
        d: dict[str, list] = defaultdict(list)
        lt_t, g_t, _ = market_events[idx]
        for logs, col in ((lt_t, 0), (g_t, 1)):
            for lg in logs:
                b, lidx, frm, to, value = A._decode_transfer(lg)
                for addr, sign in ((frm, -1), (to, 1)):
                    if addr not in excl:
                        d[vault_owner.get(addr, addr)].append(
                            (b, lidx, sign * value if col == 0 else 0,
                             sign * value if col == 1 else 0))
        deltas[idx] = d
    print(f"{len(vault_owner)} HybridVault(s) counted as their owners")

    # USD per LT and per gauge share at each sampled block
    v_lt: dict[int, list[float]] = {}
    v_g: dict[int, list[float]] = {}
    for idx, c in ctx.items():
        v_lt[idx], v_g[idx] = [], []
        for b in grid:
            cm = cache[b][idx]
            v = cm["pw"] / A.PROBE_LT * cm["btc"] / 1e18 / c["scale"]
            v_lt[idx].append(v)
            v_g[idx].append(cm["cta"] / A.PROBE_LT * v)

    # All users' balances after each sampled block, from per-market running
    # totals of every change (not from the per-user trajectories)
    pos = {b: k for k, b in enumerate(grid)}
    tot_lt: dict[int, list[int]] = {}
    tot_g: dict[int, list[int]] = {}
    for idx in ctx:
        step_lt = [0] * len(grid)
        step_g = [0] * len(grid)
        for recs in deltas[idx].values():
            for b, _li, dlt, dg in recs:
                k = 0 if b <= start_block else pos[b]  # every change is a sampled block
                step_lt[k] += dlt
                step_g[k] += dg
        run_lt = run_g = 0
        tot_lt[idx], tot_g[idx] = [], []
        for k in range(len(grid)):
            run_lt += step_lt[k]
            run_g += step_g[k]
            tot_lt[idx].append(run_lt)
            tot_g[idx].append(run_g)

    dt = [(ts[grid[k + 1]] - ts[grid[k]]) / 86400 for k in range(len(grid) - 1)]
    return SimpleNamespace(
        ctx=ctx, cache=cache, grid=grid, pos=pos, dt=dt, start_block=start_block,
        end_block=end_block, days_total=days_total, deltas=deltas, excluded=excluded,
        vaults_of=vaults_of, v_lt=v_lt, v_g=v_g, tot_lt=tot_lt, tot_g=tot_g)


def trajectory(recs: list, w) -> list[tuple[int, int, int]]:
    """One user's (grid position, LT, gauge shares) from that position on, ending
    with the last grid position, from its balance changes in one market."""
    recs.sort(key=lambda x: (x[0], x[1]))
    traj = []
    lt_run = g_run = 0
    for b, _li, dlt, dg in recs:
        if b > w.start_block and not traj:
            traj.append((0, lt_run, g_run))
        lt_run += dlt
        g_run += dg
        if b > w.start_block:
            traj.append((w.pos[b], lt_run, g_run))
    if not traj:
        traj.append((0, lt_run, g_run))
    traj.append((len(w.grid) - 1, lt_run, g_run))
    return traj


def check_chain(w) -> bool:
    """Users' LT / gauge share totals against the chain at both ends."""
    ok = True
    for label, k, b in (("start", 0, w.start_block), ("end", len(w.grid) - 1, w.end_block)):
        chain = chain_totals(b, w.ctx, w.excluded)
        bad = {i: (w.tot_lt[i][k], w.tot_g[i][k], chain[i]) for i in w.ctx
               if (w.tot_lt[i][k], w.tot_g[i][k]) != chain[i]}
        print(f"users' LT / gauge shares vs chain at {label} block {b}: "
              + ("all markets match exactly" if not bad else f"MISMATCH {bad}"))
        ok &= not bad
    return ok


def parse_args(args: list[str], default_out: str) -> tuple[list[int], str | None, int | None, str]:
    output_csv = default_out
    start = None
    end_block = None
    while "--out" in args or "--start" in args or "--end-block" in args:
        for flag in ("--out", "--start", "--end-block"):
            if flag in args:
                i = args.index(flag)
                val = args[i + 1]
                args = args[:i] + args[i + 2:]
                if flag == "--out":
                    output_csv = val
                elif flag == "--start":
                    start = val
                else:
                    end_block = int(val)
    return [int(a) for a in args], start, end_block, output_csv


def main() -> None:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        sys.exit(0)
    market_args, start, end_block, output_csv = parse_args(args, "value_integral.csv")
    w = load_window(market_args, start, end_block)
    ctx, grid, dt = w.ctx, w.grid, w.dt

    v_all = [sum(w.tot_lt[i][k] * w.v_lt[i][k] + w.tot_g[i][k] * w.v_g[i][k] for i in ctx)
             for k in range(len(grid))]
    empty = sum(dt[k] for k in range(len(dt)) if v_all[k] <= 0)
    if empty:
        print(f"⚠ {empty:.4f} days with no user value: not counted")

    # J[k] = ∫ from start to grid[k] of (value of one share) / V_all dt, so a
    # holding of s shares over [grid[a], grid[b]) adds s * (J[b] - J[a])
    j_lt: dict[int, list[float]] = {}
    j_g: dict[int, list[float]] = {}
    for idx in ctx:
        acc_lt = acc_g = 0.0
        j_lt[idx], j_g[idx] = [0.0], [0.0]
        for k in range(len(dt)):
            if v_all[k] > 0:
                acc_lt += w.v_lt[idx][k] / v_all[k] * dt[k]
                acc_g += w.v_g[idx][k] / v_all[k] * dt[k]
            j_lt[idx].append(acc_lt)
            j_g[idx].append(acc_g)

    days: dict[str, float] = defaultdict(float)
    held_in: dict[str, set[int]] = defaultdict(set)
    for idx in ctx:
        jl, jg = j_lt[idx], j_g[idx]
        for user, recs in w.deltas[idx].items():
            traj = trajectory(recs, w)
            for (ka, lt_bal, g_bal), (kb, _, _) in zip(traj, traj[1:]):
                if kb > ka and (lt_bal or g_bal):
                    days[user] += lt_bal * (jl[kb] - jl[ka]) + g_bal * (jg[kb] - jg[ka])
                    held_in[user].add(idx)

    # Invariant and reconciliation with the chain
    total = sum(days.values())
    expect = w.days_total - empty
    print(f"\nΣ days over users = {total:.9f}; window with user value = {expect:.9f} days "
          f"(relative error {abs(total - expect) / expect:.1e})")
    ok = abs(total - expect) <= 1e-9 * expect
    ok &= check_chain(w)
    if not ok:
        raise SystemExit("invariant check failed: not writing the CSV")

    df = (pl.DataFrame({
              "user": list(days),
              "days": list(days.values()),
              "markets": [" ".join(map(str, sorted(held_in[u]))) for u in days],
              "hybrid_vault": [" ".join(w.vaults_of.get(u, ())) or None for u in days],
          })
          .with_columns((pl.col("days") / expect).alias("fraction"),
                        pl.lit(w.start_block).alias("from_block"),
                        pl.lit(w.end_block).alias("to_block"))
          .select("user", "days", "fraction", "markets", "hybrid_vault",
                  "from_block", "to_block")
          .sort("user"))
    df.write_csv(output_csv)
    print(f"\nWrote {len(df)} users to {output_csv}")
    pl.Config.set_fmt_str_lengths(44)
    print(df.sort("days", descending=True).head(10).select("user", "days", "fraction", "markets"))


if __name__ == "__main__":
    main()
