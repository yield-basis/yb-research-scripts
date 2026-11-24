"""yb LT prices in the asset (WETH): the YB lending oracles and the redemption value, ported from yb-core to numpy.

Float64 ports, vectorized over samples, of:
  YBLendingOracle.price_in_asset   (yb-core contracts/utils/YBLendingOracle.vy; weth.oracles.yieldbasis.eth is the
                                    YBPriceProxy clone that forwards price() to it for the yb-WETH LT)
  YBLendingOracleLL                (contracts/utils/YBLendingOracleLL.vy; weth.oracles-ll.yieldbasis.eth): the same
                                    price with an EMA on the fundamental rate (the LT rate at price == price_scale)
  LT.preview_emergency_withdraw    (contracts/LT.vy): the position's constituents, LP share in coin0 and coin1 minus
                                    its debt share; redemption value = coin1 + (coin0 - debt) / market price
  LT._calculate_values             the admin fee / staker split that turns AMM equity into a per-LT value
  lp_oracle_2._portfolio_value     (curve_std): the D = 1 stableswap portfolio value at a price ratio, used by the
                                    LP oracle to price the LP at the pool's price_oracle

Units: coin0 (crvUSD) per coin1 (WETH) for pool prices, crvUSD for AMM values, the crvUSD aggregator in USD per
crvUSD, LT values in the asset (WETH) per LT. check_oracle_onchain.py compares these with the deployed contracts.
"""

from dataclasses import dataclass

import numpy as np

L = 2
LEV_RATIO = (L / (2 * L - 1)) ** 2  # AMM.get_x0 at leverage 2: 4 / 9
SQRT_P_MAX_DEBT = 0.75 + np.sqrt(2) / 8  # sqrt(AMM price / oracle) at debt / collateral = 17/32 (MAX_SAFE_DEBT)
SQRT_P_MIN_DEBT = 0.75 + np.sqrt(2) / 2  # ... at 1/16 (MIN_SAFE_DEBT)
MIN_STAKED_FOR_FEES = 1e-2  # LT: 10**16 wei of staked LT
SQRT_MIN_UNSTAKED_FRACTION = 1e-4  # LT: 10**14 / 10**18


def a_raw(pool_A):
    """LPOracle._scaled_A_raw_from_A: pool A (A * N**(N-1) * 10_000) -> solver A_raw (A * 10_000)."""
    return pool_A * 1e4 / (2 * 1e4)


def _x_from_y(a4, y):
    """Stableswap D = 1: x on the invariant for y; a4 = 4 * A_eff."""
    b1 = 1 - a4 * (1 - y)
    return (np.sqrt(b1 * b1 + a4 / y) - b1) / (2 * a4)


def _p_from_y(a4, y):
    x = _x_from_y(a4, y)
    t = a4 * x
    return (t + 1 / (4 * y * y)) / (t + 1 / (4 * x * y))


def portfolio_value(A_raw, p):
    """lp_oracle_2._portfolio_value: x + p y at the D = 1 stableswap point with marginal price p (p = price_oracle /
    price_scale), by bisection on the monotone branch y in (0, 1/2]; p < 1 through the reciprocal branch."""
    p = np.asarray(p, dtype=float)
    a4 = 4 * A_raw / 1e4
    q = np.where(p < 1, 1 / p, p)
    lo, hi = np.full_like(q, 1e-18), np.full_like(q, 0.5)
    for _ in range(80):
        mid = (lo + hi) / 2
        above = _p_from_y(a4, mid) > q
        lo, hi = np.where(above, mid, lo), np.where(above, hi, mid)
    y = hi
    x = _x_from_y(a4, y)
    x, y = np.where(p < 1, y, x), np.where(p < 1, x, y)
    return x + p * y


@dataclass
class OracleTerms:
    """The per-LT-independent part of both oracles: equity in crvUSD (not yet per share), and the pieces the LL
    variant and the untradable branch need."""
    tradable: np.ndarray  # x0 solvable and AMM not killed
    x0: np.ndarray  # AMM.get_x0 at the AMM's price oracle (USD collateral value, crvUSD debt)
    equity_po: np.ndarray  # tradable: x0 (factor - 1), USD; else collateral at the LP oracle price minus debt
    asset_price: np.ndarray  # USD per coin1 at price_oracle: price_oracle * agg
    value_ps: np.ndarray  # x0 / (2L - 1): the AMM value LT accounts with
    p_o_ps: np.ndarray  # USD per coin1 at price_scale: price_scale * agg


def oracle_terms(price_oracle, price_scale, virtual_price, D, pool_supply, A_raw, collateral, debt, agg, killed=False):
    """YBLendingOracle._price up to the per-share step, for arrays of pool / AMM states."""
    lp_price_ps = 2 * virtual_price * np.sqrt(price_scale)
    pv = portfolio_value(A_raw, price_oracle / price_scale)
    lp_price_oracle = pv * D / pool_supply
    p_o_amm = lp_price_ps * agg
    coll_value = p_o_amm * collateral
    disc = coll_value * coll_value - 4 * coll_value * LEV_RATIO * debt
    solvable = disc >= 0
    x0 = np.where(solvable, (coll_value + np.sqrt(np.maximum(disc, 0))) / (2 * LEV_RATIO), 0.0)
    tradable = solvable & ~np.asarray(killed, dtype=bool)

    # Where arbitrage leaves the AMM (_sqrt_arb_endpoint): the safe-debt limits can stop it short of the market
    sqrt_r = np.sqrt(lp_price_oracle / lp_price_ps)
    with np.errstate(invalid="ignore", divide="ignore"):
        sqrt_c = np.sqrt(np.maximum(x0 - debt, 0) / coll_value)
    sqrt_e = np.where(sqrt_r < sqrt_c, np.maximum(sqrt_r, np.minimum(sqrt_c, SQRT_P_MAX_DEBT)),
                      np.minimum(sqrt_r, np.maximum(sqrt_c, SQRT_P_MIN_DEBT)))
    factor = L * (sqrt_r * sqrt_r / sqrt_e + sqrt_e) / (2 * L - 1)
    equity_trade = np.where(factor > 1, x0 * (factor - 1), 0.0)
    equity_balance = np.maximum(collateral * lp_price_oracle * agg - debt, 0.0)
    return OracleTerms(tradable=tradable, x0=x0, equity_po=np.where(tradable, equity_trade, equity_balance),
                       asset_price=price_oracle * agg, value_ps=x0 / (2 * L - 1), p_o_ps=price_scale * agg)


@dataclass
class LiquidityState:
    """LT.liquidity plus the token counts it rebases: values in the asset, tokens in LT."""
    admin: float
    total: float
    ideal_staked: float
    staked: float
    supply_tokens: float
    staked_tokens: float


def calculate_values(s: LiquidityState, cur_value: float, min_admin_fee: float) -> LiquidityState:
    """LT._calculate_values: split the change of the AMM value (in the asset, at price_scale) between the admin fee,
    stakers (who only recover losses) and the other holders, by burning staked tokens (token_reduction)."""
    staked, supply = s.staked_tokens, s.supply_tokens
    f_a = 1 - (1 - min_admin_fee) * np.sqrt(1 - staked / supply)
    value_change = cur_value - (s.total + s.admin)
    v_st_loss = max(s.ideal_staked - s.staked, 0.0)
    if staked >= MIN_STAKED_FOR_FEES:
        if value_change > 0:
            v_loss = min(value_change, v_st_loss * supply / staked)
            dv_use = v_loss + (value_change - v_loss) * (1 - f_a)
        else:
            dv_use = value_change
    else:
        dv_use = value_change * (1 - f_a)
    admin = s.admin + (value_change - dv_use)
    dv_s = dv_use * staked / supply
    if dv_use > 0:
        dv_s = min(dv_s, v_st_loss)
    new_total = max(s.total + dv_use, 0.0)
    new_staked = max(s.staked + dv_s, 0.0)
    den = new_total - new_staked
    token_reduction = (new_total * staked - new_staked * supply) / den if den != 0 else 0.0
    max_reduction = abs(value_change * supply / (s.total + value_change) * (1 - f_a) / SQRT_MIN_UNSTAKED_FRACTION)
    if staked > 0:
        token_reduction = min(token_reduction, staked - 1e-18)
    token_reduction = min(token_reduction, supply - 1e-18)
    token_reduction = float(np.clip(token_reduction, -max_reduction, max_reduction))
    if den < 1e-14:
        token_reduction = max(token_reduction, 0.0)
    return LiquidityState(admin=admin, total=new_total, ideal_staked=s.ideal_staked, staked=new_staked,
                          supply_tokens=supply - token_reduction, staked_tokens=staked - token_reduction)


def per_share(lv: LiquidityState) -> float:
    """total / (total + max(admin, 0)) / supply: the share of AMM equity one LT holds (both the oracle's per-share
    step and emergency_withdraw's frac)."""
    return lv.total / (lv.total + max(lv.admin, 0.0)) / lv.supply_tokens


def redemption(collateral, debt, balance0, balance1, pool_supply, share, market_price):
    """LT.preview_emergency_withdraw for one LT: (coin1 out, signed coin0 out) and their value in coin1 at
    market_price (coin0 per coin1)."""
    lp = collateral * share
    coin1 = lp * balance1 / pool_supply
    coin0 = lp * balance0 / pool_supply - debt * share
    return coin1, coin0, coin1 + coin0 / market_price


def ll_ema(fundamental, t, ema_time):
    """YBLendingOracleLL._price_w at every sample: the EMA advanced with the previous checkpoint's fundamental
    (fundamental_last), seeded with the first; a checkpoint per sample."""
    ema = np.empty_like(fundamental)
    ema[0] = fundamental[0]
    alpha = np.exp(-np.diff(t) / ema_time)
    for i in range(1, len(fundamental)):
        ema[i] = fundamental[i - 1] * (1 - alpha[i - 1]) + ema[i - 1] * alpha[i - 1]
    return ema
