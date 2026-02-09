#!/usr/bin/env python3
"""
StableSwap portfolio value solver.

Given invariant D, marginal price p, and amplification _amp,
finds V = xp[0] + p * xp[1], where (xp[0], xp[1]) satisfy
the StableSwap invariant and price condition.

Uses Newton's method with numerical differentiation.
All arithmetic in wad integers (1.0 == 10**18).
"""

N_COINS = 2
A_MULTIPLIER = 10000
WAD = 10**18


# ──────────── Ported from StableswapMath.vy ────────────

def newton_D(_amp: int, _xp: list) -> int:
    S = sum(_xp)
    if S == 0:
        return 0
    D = S
    Ann = _amp * N_COINS
    for _ in range(255):
        D_P = D
        for x in _xp:
            D_P = D_P * D // x
        D_P //= N_COINS ** N_COINS
        Dprev = D
        D = (
            (Ann * S // A_MULTIPLIER + D_P * N_COINS) * D
            // ((Ann - A_MULTIPLIER) * D // A_MULTIPLIER + (N_COINS + 1) * D_P)
        )
        if D > Dprev:
            if D - Dprev <= 1:
                return D
        else:
            if Dprev - D <= 1:
                return D
    raise RuntimeError("newton_D did not converge")


def get_y(A: int, xp: list, D: int, i: int) -> int:
    assert 0 <= i < N_COINS
    S_ = 0
    c = D
    Ann = A * N_COINS
    for j in range(N_COINS):
        if j == i:
            continue
        _x = xp[j]
        S_ += _x
        c = c * D // (_x * N_COINS)
    c = c * D * A_MULTIPLIER // (Ann * N_COINS)
    b = S_ + D * A_MULTIPLIER // Ann
    y = D
    for _ in range(255):
        y_prev = y
        y = (y * y + c) // (2 * y + b - D)
        if y > y_prev:
            if y - y_prev <= 1:
                return y
        else:
            if y_prev - y <= 1:
                return y
    raise RuntimeError("get_y did not converge")


def get_p(_xp: list, _D: int, _A_gamma: list) -> int:
    ANN = _A_gamma[0] * N_COINS
    Dr = _D // (N_COINS ** N_COINS)
    for i in range(N_COINS):
        Dr = Dr * _D // _xp[i]
    xp0_A = ANN * _xp[0] // A_MULTIPLIER
    return WAD * (xp0_A + Dr * _xp[0] // _xp[1]) // (xp0_A + Dr)


# ──────────── Portfolio value solver ────────────

def portfolio_value(D: int, p: int, _amp: int) -> tuple:
    """
    Compute V = xp[0] + p * xp[1] // WAD.

    Uses Newton's method to find x1 such that get_p == p,
    with x0 determined from the invariant via get_y.

    Parameters (all wad integers):
        D     : invariant (from newton_D)
        p     : marginal price dx0/dx1 in wad (10**18 == 1.0)
        _amp  : amplification parameter (A * N**(N-1) * A_MULTIPLIER)

    Returns:
        (V, iters) where V = x0 + p * x1 // WAD  (wad integer)
    """
    if p == WAD:
        return D, 0  # balanced pool: x0 = x1 = D/2, V = D

    # --- Phase 1: Bisection for a rough bracket (10 steps) ---
    # Price is monotonically decreasing in x1.
    x1_lo = 1
    x1_hi = D - 1

    iters = 0
    for _ in range(10):
        iters += 1
        x1_mid = (x1_lo + x1_hi) // 2
        x0_mid = get_y(_amp, [0, x1_mid], D, 0)
        p_mid = get_p([x0_mid, x1_mid], D, [_amp, 0])
        if p_mid > p:
            x1_lo = x1_mid
        else:
            x1_hi = x1_mid

    # --- Phase 2: Newton's method from the bisection midpoint ---
    x1 = (x1_lo + x1_hi) // 2

    for _ in range(255):
        x0 = get_y(_amp, [0, x1], D, 0)
        p_cur = get_p([x0, x1], D, [_amp, 0])

        dp = p_cur - p
        if abs(dp) <= 1:
            break
        iters += 1

        # Numerical derivative dp/dx1
        h = max(x1 // 10_000_000, 1)
        x0_h = get_y(_amp, [0, x1 + h], D, 0)
        p_h = get_p([x0_h, x1 + h], D, [_amp, 0])
        slope_h = p_h - p_cur

        if slope_h == 0:
            h = max(x1 // 10_000, 100)
            x0_h = get_y(_amp, [0, x1 + h], D, 0)
            p_h = get_p([x0_h, x1 + h], D, [_amp, 0])
            slope_h = p_h - p_cur
            if slope_h == 0:
                break

        x1_new = x1 - dp * h // slope_h
        # Keep within bracket
        x1_new = max(x1_lo, min(x1_hi, x1_new))
        if x1_new == x1:
            break
        x1 = x1_new

    x0 = get_y(_amp, [0, x1], D, 0)
    return x0 + p * x1 // WAD, iters


# ──────────── Tests ────────────

def test_portfolio_value():
    test_cases = [
        # (x0, x1, amp, description)
        # A = amp / (2 * A_MULTIPLIER)

        # Balanced pools
        (WAD, WAD, 2_000_000, "balanced, A=100"),
        (WAD, WAD, 100_000, "balanced, A=5"),
        (WAD, WAD, 20_000_000, "balanced, A=1000"),

        # Mildly imbalanced
        (12 * WAD // 10, 8 * WAD // 10, 2_000_000, "mild p>1, A=100"),
        (8 * WAD // 10, 12 * WAD // 10, 2_000_000, "mild p<1, A=100"),
        (11 * WAD // 10, 9 * WAD // 10, 20_000_000, "mild p>1, A=1000"),

        # Moderately imbalanced
        (15 * WAD // 10, 5 * WAD // 10, 2_000_000, "moderate p>1, A=100"),
        (5 * WAD // 10, 15 * WAD // 10, 2_000_000, "moderate p<1, A=100"),

        # Heavily imbalanced
        (2 * WAD, WAD // 2, 100_000, "heavy p>1, A=5"),
        (WAD // 2, 2 * WAD, 100_000, "heavy p<1, A=5"),
        (3 * WAD, WAD // 3, 100_000, "very heavy, A=5"),
    ]

    print(f"{'Case':<25s}  {'D':>12s}  {'p':>12s}  "
          f"{'V_expected':>14s}  {'V_computed':>14s}  {'err':>5s} {'it':>3s}")
    print("-" * 100)

    all_passed = True
    for x0, x1, amp, desc in test_cases:
        D = newton_D(amp, [x0, x1])
        p = get_p([x0, x1], D, [amp, 0])
        V_expected = x0 + p * x1 // WAD
        V_computed, iters = portfolio_value(D, p, amp)
        err = abs(V_computed - V_expected)

        status = "OK" if err <= 100 else "FAIL"
        print(f"{desc:<25s}  {D / WAD:12.6f}  {p / WAD:12.6f}  "
              f"{V_expected / WAD:14.10f}  {V_computed / WAD:14.10f}  "
              f"{err:5d} {iters:3d} {status}")

        if err > 100:
            all_passed = False

    print()
    if all_passed:
        print("All tests passed!")
    else:
        print("Some tests FAILED!")
    return all_passed


if __name__ == "__main__":
    test_portfolio_value()
