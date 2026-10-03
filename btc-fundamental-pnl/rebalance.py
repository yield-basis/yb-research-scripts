"""What the system would be worth after balancing out: the non-inflated value.

When a pool's price_scale sits away from the market price p, the books (LP valued
at price_scale) overstate what the position is worth. This module prices getting
back to balance, per sampled block, at the current market price:

    price_scale walks from its current value to p in `steps` geometric steps
    (the pools themselves move in steps: v2.1.0d by ~1/5 of the gap per rebalance,
    v3 by <= 0.6%). After every step:
      1. Pool: arbitrageurs trade its spot back to p, stopping once within the fee;
         the fee on what they take out stays in the pool (value returned).
      2. LEVAMM: its oracle is the pool's book LP price (D / supply at the current
         price_scale); arbitrageurs trade its constant-product curve
         x * y = (x0 - debt) * collateral until its price is within the AMM fee of
         the LP's real value, (balances at p) / supply. The fee stays in the AMM,
         and x0 is re-derived after each trade, as AMM.exchange does.
    At the end price_scale = p, the pool is balanced, the LEVAMM is at equilibrium,
    and donation shares are burned the way tweak_price does (keep virtual_price at
    max(floor, vp)), handing that value to the remaining LPs.

    E_bal = x0_final / (2L - 1) / p        (BTC)

steps=1 is the pessimistic bound: one jump of price_scale, so the pool sells its
whole excess along its curve and the LEVAMM is forced to rebalance in one go.

The YB pools use StableswapMath (gamma unused): on price-scaled balances
    Ann * (x0 + x1) + D = Ann * D + D^3 / (4 x0 x1),  Ann = A() * 2 / 10000
so D comes from the contract's Newton iteration, get_y has a closed form and the
spot price is get_p's formula. Everything is vectorised in float64 (relative
error ~1e-12 against the pools' stored D).
"""
from __future__ import annotations

import numpy as np

A_MULTIPLIER = 10_000
BISECT = 60


def ann(amp):
    """Effective stableswap Ann from the pool's A() (already * A_MULTIPLIER * N)."""
    return np.asarray(amp, dtype=float) * 2 / A_MULTIPLIER


def ss_D(x0, x1, a):
    """StableswapMath.newton_D in float64."""
    s = x0 + x1
    d = s.copy()
    for _ in range(40):
        d_p = d**3 / (4 * x0 * x1)
        d = (a * s + 2 * d_p) * d / ((a - 1) * d + 3 * d_p)
    return d


def ss_y(x, d, a):
    """StableswapMath.get_y in closed form: the other balance for balance x."""
    b = x + d / a - d
    c = d**3 / (4 * x * a)
    return (-b + np.sqrt(b * b + 4 * c)) / 2


def ss_spot(x0, x1, d, a):
    """StableswapMath.get_p: marginal price of coin 1 in coin 0 (scaled units)."""
    dr = d**3 / (4 * x0 * x1)
    return (a * x0 + dr * x0 / x1) / (a * x0 + dr)


def pool_arbitrage(x0, x1, a, fee, q):
    """Arbitrage the pool until its scaled spot price is within the fee of q.

    x0, x1 are price-scaled balances; q = p / price_scale is the fair scaled price
    of coin 1. Arbitrageurs remove the cheap coin; the fee on it stays in the pool.
    Returns new (x0, x1) and the fee income (coin-0 units).
    """
    d = ss_D(x0, x1, a)
    s = ss_spot(x0, x1, d, a)
    buy1 = s < q * (1 - fee)        # coin 1 cheap: remove coin 1, add coin 0
    buy0 = s > q / (1 - fee)        # coin 0 cheap: remove coin 0, add coin 1
    # bisection on the removed coin's balance (spot moves monotonically with it)
    rem = np.where(buy1, x1, x0)
    lo, hi = rem * 1e-6, rem.copy()
    target = np.where(buy1, q * (1 - fee), q / (1 - fee))
    for _ in range(BISECT):
        mid = (lo + hi) / 2
        other = ss_y(mid, d, a)
        sm = np.where(buy1, ss_spot(other, mid, d, a), ss_spot(mid, other, d, a))
        # removing coin 1 raises its price; removing coin 0 lowers coin 1's price
        too_far = np.where(buy1, sm > target, sm < target)
        lo = np.where(too_far, mid, lo)
        hi = np.where(too_far, hi, mid)
    new_rem = hi
    new_other = ss_y(new_rem, d, a)
    fee_amt = fee * (rem - new_rem)
    nx0 = np.where(buy1, new_other, np.where(buy0, new_rem + fee_amt, x0))
    nx1 = np.where(buy1, new_rem + fee_amt, np.where(buy0, new_other, x1))
    fee_income = np.where(buy1, fee_amt * q, np.where(buy0, fee_amt, 0.0))
    return nx0, nx1, fee_income


def get_x0(p_o, coll, debt, lev_ratio):
    """AMM.get_x0 (safe_limits=False) in float64."""
    cv = p_o * coll
    disc = np.maximum(cv * cv - 4 * cv * lev_ratio * debt, 0.0)
    return (cv + np.sqrt(disc)) / (2 * lev_ratio)


def levamm_arbitrage(p_o, p_ext, coll, debt, lev_ratio, fee, rounds=30):
    """Trade the LEVAMM (oracle p_o) until its price is within fee of p_ext."""
    for _ in range(rounds):
        x0 = get_x0(p_o, coll, debt, lev_ratio)
        xi = x0 - debt
        k = xi * coll
        with np.errstate(divide="ignore", invalid="ignore"):
            # traders sell LP to the AMM while its post-fee bid beats p_ext
            y_new = np.sqrt(k * (1 - fee) / p_ext)
            sell = (y_new > coll * (1 + 1e-12)) & (coll > 0)
            out_s = (xi - k / y_new) * (1 - fee)
            # traders buy LP from the AMM while its post-fee ask is below p_ext
            x_new = np.sqrt(p_ext * (1 - fee) * k)
            buy = (x_new > xi * (1 + 1e-12)) & (coll > 0)
            out_b = (coll - k / x_new) * (1 - fee)
        debt = np.where(sell, debt + out_s, np.where(buy, debt - (x_new - xi), debt))
        coll = np.where(sell, y_new, np.where(buy, coll - out_b, coll))
    return coll, debt


def balance_out(*, b0, b1, ps, p, amp, pool_fee, supply, donation_shares, vp, vp_floor,
                coll, debt, leverage, amm_fee, steps=40):
    """E_bal in BTC for arrays of samples (all inputs floats in natural units).

    b0 (crvUSD) and b1 (BTC) are raw pool balances / decimals, ps the pool's
    price_scale and p the market price (crvUSD per BTC), vp / vp_floor its actual
    and guaranteed virtual price, coll the AMM's LP, debt its crvUSD debt.
    """
    a = ann(amp)
    lev_ratio = leverage**2 / (2 * leverage - 1) ** 2
    b0, b1, coll, debt = (np.array(v, dtype=float) for v in (b0, b1, coll, debt))
    pool_fees = np.zeros_like(b0)
    for k in range(steps + 1):
        ps_k = ps * (p / ps) ** (k / steps)
        x0, x1, fee_in = pool_arbitrage(b0, b1 * ps_k, a, pool_fee, p / ps_k)
        b0, b1 = x0, x1 / ps_k
        pool_fees += fee_in
        burn = 0.0
        if k == steps:  # final state: burn donation shares as tweak_price would
            value = b0 + b1 * p
            tweaked_supply = value / (2 * np.sqrt(p)) / np.maximum(vp_floor, vp)
            burn = np.clip(supply - tweaked_supply, 0, donation_shares)
        shares = supply - burn
        p_book = ss_D(b0, b1 * ps_k, a) / shares          # LEVAMM oracle (agg = 1)
        p_ext = (b0 + b1 * p) / shares                     # LP's real value at p
        coll, debt = levamm_arbitrage(p_book, p_ext, coll, debt, lev_ratio, amm_fee)
    x0 = get_x0(p_book, coll, debt, lev_ratio)
    return {"e_bal": x0 / (2 * leverage - 1) / p, "pool_fee_income": pool_fees,
            "lp_price": p_ext}
