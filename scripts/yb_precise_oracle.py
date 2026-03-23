# Script which tests an oracle to find a lower estimate for LT in YB pool
import pylab

import math
from datetime import datetime
import numpy as np
from brownie import Contract, config
from brownie import web3
from brownie import multicall

config['autofetch_sources'] = True


FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"

START_BLOCK = 23784145 + 100
N_POINTS = 500
POOL_ID = 4


class FXSwapLPOracleSim:
    """Float simulation of FXSwapLPOracle math (no fixed-point precisions)."""

    BISECTION_ITERS = 128
    PRICE_TOL_REL = 1e-6

    def __init__(self, pool):
        self.pool = pool
        self.A = pool.A() / 10000
        self.decimals0 = 10 ** Contract(pool.coins(0)).decimals()
        self.decimals1 = 10 ** Contract(pool.coins(1)).decimals()

        self.ema0_oracle = None
        self.ema0_scale = None

    def read_state(self):
        # Read this first to get it via multicall
        self._price_oracle = self.pool.price_oracle()
        self._price_scale = self.pool.price_scale()
        self._virtual_price = self.pool.get_virtual_price()
        self._balances_0 = self.pool.balances(0)
        self._balances_1 = self.pool.balances(1)
        self._supply = self.pool.totalSupply()

        self._price_oracle /= 1e18
        self._price_scale /= 1e18
        self._virtual_price /= 1e18
        self._balances_0 /= self.decimals0
        self._balances_1 /= self.decimals1
        self._supply /= 1e18

    def get_price(self, method: str):
        match method:
            case 'actual_portfolio_value':
                return self.actual_portfolio_value([self._balances_0, self._balances_1], self._price_oracle, self._supply)
            case 'lp_price':
                return self.lp_price(self._price_oracle, self._price_scale, [self._balances_0, self._balances_1], self._supply)

    @classmethod
    def actual_portfolio_value(cls, tokens: list, price_oracle: float, total_supply: float):
        return (tokens[0] + price_oracle * tokens[1]) / total_supply

    def lp_price(self, price_oracle: float, price_scale: float, tokens: list, total_supply: float) -> float:
        p_scaled = price_oracle / price_scale
        balances = self._get_x_y(self.A, p_scaled)

        D = self._get_D(tokens[0], price_scale * tokens[1])

        return (balances[0] + p_scaled * balances[1]) * D / total_supply

    def _get_D(self, x: float, y: float, *, max_iters: int = 255, tol: float = 1e-12) -> float:
        S = x + y
        D = S
        Ann = 4.0 * self.A

        for _ in range(max_iters):
            D_P = D * D / (2.0 * x)
            D_P = D_P * D / (2.0 * y)
            D_next = ((Ann * S + 2.0 * D_P) * D) / ((Ann - 1.0) * D + 3.0 * D_P)
            if abs(D_next - D) <= tol * max(1.0, D_next):
                return D_next
            D = D_next

        raise RuntimeError("D didn't converge")

    @classmethod
    def _x_from_y(cls, A: float, y: float) -> float:
        b1 = 1.0 + 4.0 * A * (y - 1.0)
        term = 4.0 * A / y
        rad = math.sqrt(b1 * b1 + term)
        if rad <= b1:
            return 0.0
        return (rad - b1) / (8.0 * A)

    @classmethod
    def _p_from_y(cls, A: float, y: float) -> float:
        x = cls._x_from_y(A, y)
        if x <= 0:
            return math.inf

        term4a = 4.0 * A
        num = term4a + 1.0 / (4.0 * x * y * y)
        den = term4a + 1.0 / (4.0 * x * x * y)
        return num / den

    @classmethod
    def _y_from_bisection(cls, A: float, p: float) -> float:
        if p < 1.0:
            raise ValueError("p must be >= 1 for bisection branch")

        lo = 1e-12
        hi = 0.5

        for _ in range(cls.BISECTION_ITERS):
            mid = (lo + hi) / 2.0
            pm = cls._p_from_y(A, mid)
            tol_abs = max(p * cls.PRICE_TOL_REL, 1e-15)

            if pm > p:
                if pm - p <= tol_abs:
                    return mid
                lo = mid
            else:
                if p - pm <= tol_abs:
                    return mid
                hi = mid

            if hi - lo <= 1e-15:
                return hi

        raise RuntimeError("Didn't converge")

    @classmethod
    def _get_x_y(cls, A: float, p: float) -> tuple[float, float]:
        if p < 1.0:
            p_inv = 1.0 / p
            y_inv = cls._y_from_bisection(A, p_inv)
            x_inv = cls._x_from_y(A, y_inv)
            return y_inv, x_inv

        y = cls._y_from_bisection(A, p)
        x = cls._x_from_y(A, y)
        return x, y

    @classmethod
    def _portfolio_value(cls, A: float, p: float) -> float:
        x, y = cls._get_x_y(A, p)
        return x + p * y


def main():
    mc = Contract("0x5BA1e12693Dc8F9c48aAD8770482f4739bEeD696")

    factory = Contract(FACTORY)
    market = factory.markets(POOL_ID)
    amm = Contract(market[2])
    lt = Contract(market[3])
    cryptopool = Contract(amm.COLLATERAL())

    current_block = web3.eth.block_number
    blocks = [int(b) for b in np.linspace(START_BLOCK, current_block, N_POINTS)]

    ps_lp_oracle = Contract(amm.PRICE_ORACLE_CONTRACT())
    lp_oracle = FXSwapLPOracleSim(cryptopool)

    times = []
    portfolio_values = []
    oracle_values = []

    for b in blocks:
        with multicall(address=mc.address, block_identifier=b):
            t = mc.getCurrentBlockTimestamp()
            lp_oracle.read_state()
            ps_lp_price = ps_lp_oracle.price()
            value_oracle = amm.value_oracle()
            yb_state = amm.get_state()
            liquidity = lt.liquidity()
            supply = lt.totalSupply()

        ps_lp_price /= 1e18
        value_oracle = value_oracle[1] / 1e18
        supply /= 1e18

        collateral, debt, x0 = yb_state
        x0 /= 1e18
        collateral /= 1e18
        debt /= 1e18

        admin, total, ideal_staked, staked = liquidity
        f_lp = total / (admin + total)

        t = datetime.fromtimestamp(t)
        print(b, t)
        times.append(t)

        lp_price_oracle = lp_oracle.get_price('lp_price')

        L = 2
        yb_oracle_value = x0 * (2 * L / (2*L - 1) * (lp_price_oracle / ps_lp_price)**0.5 - 1)
        yb_oracle_value *= f_lp / supply / lp_oracle._price_oracle

        oracle_values.append(yb_oracle_value)

        redemption_value = lp_oracle.get_price('actual_portfolio_value') * collateral - debt
        redemption_value *= f_lp / supply / lp_oracle._price_oracle
        portfolio_values.append(redemption_value)

    pylab.plot(times, portfolio_values, c="black", label="Portfolio value")
    pylab.plot(times, oracle_values, c="red", label="LP oralce value")

    pylab.title(f'Price oracle for pool {lt.symbol()}')
    pylab.grid()
    pylab.legend()
    pylab.xticks(rotation=45, ha='right')
    pylab.xlabel('Time')
    pylab.ylabel('LP price [pool asset]')

    pylab.tight_layout()
    pylab.show()
