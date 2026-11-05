"""Variant of value_integral.py: v2 time counts only for what was carried into v3.

    days(user) = ∫ W_user(t) / W_all(t) dt        (t in days)
    W_user(t)  = Σ_asset [ min(V2(t), A3) + V3(t) ]        in USD

per user, for BTC (WBTC, cbBTC and tBTC together, 1:1) and for WETH:
    V2(t), V3(t)  redemption value in the asset, unstaked LT plus gauge shares,
                  over the asset's v2 / v3 markets at t
    A3            V3 averaged over the v3 markets' whole life (deploy to the
                  end block, time with nothing deposited counts as zero)

The min is taken in the asset. When it binds, all the user's v2 positions in
that asset scale down by the same factor, and each converts to USD at its own
pool's price at t as in value_integral.py. v1 is not counted at all. So a user
who never held the asset in v3 gets nothing for v2, a small v3 deposit gives a
small v2 allocation, and v3 deposits count in full. Moving from one BTC wrapper
in v2 to another in v3 counts as migrating.

Same window as value_integral.py, but time with nothing countable (before the
first eligible v2 deposit) is not part of the total.

Checks (no CSV is written if one fails):
  - Σ days = the time with something countable, in days. W_all's v3 part comes
    from running totals, each user's part from its own trajectory.
  - at every sampled block, all users' uncapped v2 value from their
    trajectories equals the running totals; the cap only applies on top of it.
  - Σ users' A3 per market equals the market's average users' value.
  - users' LT / gauge shares vs the chain at both ends.

Run all_users_pnl.py first. value_integral.csv and its script stay as they are.

Usage:
    uv run python scripts/value_integral_v3cap.py [--start "2025-10-15 09:42:23"]
                                                  [--end-block N] [--out FILE]
    # default output value_integral_v3cap.csv
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

import numpy as np
import polars as pl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import all_users_pnl as A  # noqa: E402
import value_integral as V  # noqa: E402
from yb import version  # noqa: E402


def asset_family(sym: str) -> str:
    """The unit the cap is taken in: all BTC wrappers are one asset."""
    return "BTC" if sym.endswith("BTC") else sym


def compute(w, capped: bool = True):
    """Returns per-user results and the check values. capped=False lifts the cap,
    leaving a plain linear integral over v2 + v3 to test this code path against."""
    ctx, grid = w.ctx, w.grid
    n = len(grid) - 1  # steps: the state at grid[k] holds for dt[k]
    dt = np.array(w.dt)
    v2 = [i for i in ctx if version(i) == "v2"]
    v3 = [i for i in ctx if version(i) not in ("v1", "v2")]

    # Per market: asset per LT and per gauge share, USD per asset, at each step
    r, rg, px = {}, {}, {}
    for i in (*v2, *v3):
        samples = [w.cache[b][i] for b in grid[:-1]]
        r[i] = np.array([s["pw"] / A.PROBE_LT / ctx[i]["scale"] for s in samples])
        rg[i] = r[i] * np.array([s["cta"] / A.PROBE_LT for s in samples])
        px[i] = np.array([s["btc"] / 1e18 for s in samples])
    def segments(traj):
        """Intervals with a position: (start, end, LT, gauge shares)."""
        return [(ka, kb, float(lt), float(g))
                for (ka, lt, g), (kb, _, _) in zip(traj, traj[1:]) if kb > ka and (lt or g)]

    segs = {i: {u: segments(V.trajectory(recs, w)) for u, recs in w.deltas[i].items()}
            for i in (*v2, *v3)}
    family = {i: asset_family(ctx[i]["sym"]) for i in (*v2, *v3)}
    v2_of: dict[str, list[int]] = defaultdict(list)
    for i in v2:
        v2_of[family[i]].append(i)

    # A3: each user's average v3 value in the asset over the market's life
    a3: dict[tuple[str, str], float] = defaultdict(float)
    a3_err = 0.0
    for i in v3:
        k0 = w.pos[ctx[i]["deploy"]] if ctx[i]["deploy"] > w.start_block else 0
        span = dt[k0:].sum()
        i_lt = np.concatenate(([0.0], np.cumsum(r[i] * dt)))
        i_g = np.concatenate(([0.0], np.cumsum(rg[i] * dt)))
        market_sum = 0.0
        for user, user_segs in segs[i].items():
            avg = sum(lt * (i_lt[kb] - i_lt[ka]) + g * (i_g[kb] - i_g[ka])
                      for ka, kb, lt, g in user_segs) / span
            a3[(user, family[i])] += avg
            market_sum += avg
        tvl = (np.array(w.tot_lt[i][:n], dtype=float) * r[i]
               + np.array(w.tot_g[i][:n], dtype=float) * rg[i])
        expect = (tvl * dt).sum() / span
        a3_err = max(a3_err, abs(market_sum - expect) / expect)

    def capped_usd(user: str, fam: str) -> tuple[int, np.ndarray]:
        """(first step, USD weight per step) of the user's v2 positions in fam:
        the positions' total in the asset is capped, each keeps its own price."""
        held = [(i, s) for i in v2_of[fam] for s in segs[i].get(user, ())]
        lo, hi = min(s[0] for _, s in held), max(s[1] for _, s in held)
        val, usd = np.zeros(hi - lo), np.zeros(hi - lo)
        for i, (ka, kb, lt, g) in held:
            v = lt * r[i][ka:kb] + g * rg[i][ka:kb]
            val[ka - lo:kb - lo] += v
            usd[ka - lo:kb - lo] += v * px[i][ka:kb]
        cap = a3.get((user, fam), 0.0) if capped else np.inf
        scale = np.divide(cap, val, out=np.ones_like(val), where=val > 0)
        return lo, usd * np.minimum(scale, 1.0)

    # Users with something to count in v2, per asset
    eligible = {fam: [u for u in {u for i in ms for u, s in segs[i].items() if s}
                      if not capped or a3.get((u, fam), 0.0) > 0]
                for fam, ms in v2_of.items()}

    # W_all: capped v2 from each user, v3 from the running totals
    w_all = np.zeros(n)
    for fam, users in eligible.items():
        for user in users:
            lo, usd = capped_usd(user, fam)
            w_all[lo:lo + len(usd)] += usd
    v2_all = np.zeros(n)  # uncapped, to test the trajectories block by block
    for i in v2:
        for user_segs in segs[i].values():
            for ka, kb, lt, g in user_segs:
                v2_all[ka:kb] += (lt * r[i][ka:kb] + g * rg[i][ka:kb]) * px[i][ka:kb]
    v2_totals = sum(np.array(w.tot_lt[i][:n], dtype=float) * np.array(w.v_lt[i][:n])
                    + np.array(w.tot_g[i][:n], dtype=float) * np.array(w.v_g[i][:n]) for i in v2)
    live = v2_totals > 0
    v2_err = float(np.max(np.abs(v2_all[live] - v2_totals[live]) / v2_totals[live]))
    for i in v3:
        w_all += (np.array(w.tot_lt[i][:n], dtype=float) * np.array(w.v_lt[i][:n])
                  + np.array(w.tot_g[i][:n], dtype=float) * np.array(w.v_g[i][:n]))

    counted = w_all > 0
    per_usd = np.where(counted, dt / np.where(counted, w_all, 1.0), 0.0)  # days per USD of weight

    days_v2: dict[str, float] = defaultdict(float)
    days_v3: dict[str, float] = defaultdict(float)
    held_in: dict[str, set[int]] = defaultdict(set)
    for fam, users in eligible.items():
        for user in users:
            lo, usd = capped_usd(user, fam)
            days_v2[user] += float((usd * per_usd[lo:lo + len(usd)]).sum())
            held_in[user].update(i for i in v2_of[fam] if segs[i].get(user))
    for i in v3:
        j_lt = np.concatenate(([0.0], np.cumsum(np.array(w.v_lt[i][:n]) * per_usd)))
        j_g = np.concatenate(([0.0], np.cumsum(np.array(w.v_g[i][:n]) * per_usd)))
        for user, user_segs in segs[i].items():
            for ka, kb, lt, g in user_segs:
                days_v3[user] += lt * (j_lt[kb] - j_lt[ka]) + g * (j_g[kb] - j_g[ka])
                held_in[user].add(i)

    return {"days_v2": days_v2, "days_v3": days_v3, "held_in": held_in, "a3": a3,
            "expect": float(dt[counted].sum()), "first": grid[int(np.argmax(counted))],
            "a3_err": a3_err, "v2_err": v2_err, "families": sorted(v2_of),
            "per_usd": per_usd}


def main() -> None:
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__)
        sys.exit(0)
    _, start, end_block, output_csv = V.parse_args(args, "value_integral_v3cap.csv")
    w = V.load_window([], start, end_block)
    res = compute(w)
    days_v2, days_v3, expect = res["days_v2"], res["days_v3"], res["expect"]

    users = sorted(days_v2.keys() | days_v3.keys())
    days = {u: days_v2.get(u, 0.0) + days_v3.get(u, 0.0) for u in users}
    total = sum(days.values())
    print(f"\nfirst countable block {res['first']}; "
          f"{w.days_total - expect:.4f} days of the window had nothing to count")
    print(f"Σ days over users = {total:.9f}; time with countable value = {expect:.9f} days "
          f"(relative error {abs(total - expect) / expect:.1e})")
    print(f"all users' uncapped v2 value vs running totals, worst block: {res['v2_err']:.1e}")
    print(f"Σ users' v3 averages vs market averages, worst market: {res['a3_err']:.1e}")
    ok = (abs(total - expect) <= 1e-9 * expect and res["v2_err"] <= 1e-9
          and res["a3_err"] <= 1e-9)
    ok &= V.check_chain(w)
    if not ok:
        raise SystemExit("invariant check failed: not writing the CSV")

    df = (pl.DataFrame({
              "user": users,
              "days": [days[u] for u in users],
              "days_v2": [days_v2.get(u, 0.0) for u in users],
              "days_v3": [days_v3.get(u, 0.0) for u in users],
              **{f"v3_avg_{fam}": [res["a3"].get((u, fam), 0.0) for u in users]
                 for fam in res["families"]},
              "markets": [" ".join(map(str, sorted(res["held_in"][u]))) for u in users],
              "hybrid_vault": [" ".join(w.vaults_of.get(u, ())) or None for u in users],
          })
          .with_columns((pl.col("days") / expect).alias("fraction"),
                        pl.lit(w.start_block).alias("from_block"),
                        pl.lit(w.end_block).alias("to_block"))
          .select("user", "days", "fraction", "days_v2", "days_v3",
                  *[f"v3_avg_{f}" for f in res["families"]], "markets", "hybrid_vault",
                  "from_block", "to_block"))
    df.write_csv(output_csv)
    print(f"\nWrote {len(df)} users to {output_csv}")
    pl.Config.set_fmt_str_lengths(44)
    pl.Config.set_tbl_cols(8)
    print(df.sort("days", descending=True).head(10)
            .select("user", "days", "fraction", "days_v2", "days_v3", "markets"))


if __name__ == "__main__":
    main()
