# Supply-sink incentive model — covering net pressure with scrvUSD

When LevAMM runs a positive **net pressure** (a crvUSD shortfall in the supply
sink — see `REPORT_net_pressure.md`), the idea is to pay a temporary bonus APR on
scrvUSD. That raises its APR, crvUSD depositors arrive (slowly), and the
incremental sink absorbs the shortfall. We want to **cover the positive pressure
while spending as little as possible** — and not much more than needed.

`incentive_sim.py` is a testbed: it drives a depositor-dynamics model with the
net-pressure signal, lets a controller set the incentive, and an optimiser tunes
the controller to minimise spend at a target coverage.

All quantities are in **normalised units** — `P` (pressure) and `S` (sink) are
fractions of half-TVL, i.e. the same units as `net_pressure`, so results are
scale-free (multiply by half-TVL for dollars).

## Model

Measured anchors (`REPORT_pool_apr_response.md`, `REPORT_liquidity_response.md`):

* inflow time constant **τ_in ≈ 9 d** (deposits arrive slowly), outflow
  **τ_out ≈ 4.5 d** (capital leaves faster);
* **hysteresis / dead-band**: crvUSD depositors don't move until scrvUSD APR ≥
  **2×** the market norm; base scrvUSD APR ≈ 1× market;
* **market norm = Aave USDC APR** — the only series covering 2024–2026 (incl. the
  2024-08-05 stress event). It is spiky (unlike sUSDS), so it is **EMA-smoothed**
  with a 7-day time constant: depositors react to a trailing average, not intraday
  spikes.

| element | rule |
|---------|------|
| signal | `P(t) = max(0, net_pressure)` |
| controller | `S*(t) = f(P, S, state)` — the sink we aim to attract |
| offer | `x(t) = 2 + S*/β` — advertised APR as a multiple of market (must clear the 2× dead-band, plus more for volume) |
| dynamics | `dS/dt = (S* − S)/τ`, τ = τ_in if S*>S else τ_out |
| spend | `(x − 1)·m·S` — bonus APR above the 1× base, paid **only on the attracted sink S** |

Cost `J = spend + λ·undercoverage_area`. Overshoot is self-penalising (a larger
`S*` costs more spend), so only undercoverage needs an explicit weight.

**β (deposit elasticity)** is the key unknown: the sink (frac of half-TVL)
attracted per unit of excess APR ratio above the 2× threshold. High β → crvUSD
floods in for a small bump (cheap); low β → you must pay a lot. We only have a
rough anchor for it (the $2M→$68M pool ramp), so we sweep it.

We deliberately do **not** add a smoothness/rate-limit penalty: an incentive
contract can re-rate arbitrarily fast, and the depositor lag (τ≈9 d) already
low-pass-filters the offer — a brief APR spike pulls in almost no extra crvUSD and
costs almost nothing (spend is charged on the slow realised sink). The system's
own inertia is the filter.

## Result — PI controller on the worst candidate

`mf120_of163` (the parameter set with the largest excursions). Optimised PI,
β = 0.5:

![PI controller](pics/incentive_pi.png)

* coverage **98.7%** of the pressure area, spend **0.113%/yr** of half-TVL;
* the sink tracks the multi-day humps but **misses the sharp 2024-08-05 tip**
  (peak deficit ~15%) — slow deposits cannot catch a 2-hour spike no matter the
  APR. This is the τ limit, and it confirms the design targets the **sustained**
  component, not the instantaneous peak.

## Sensitivity to β

Re-optimising the controller across the elasticity range:

![spend vs beta](pics/incentive_beta_sweep.png)

| β | coverage | spend %/yr | peak deficit | mean offer |
|---|----------|-----------:|-------------:|-----------:|
| 0.10 (inelastic) | 97.9% | 0.374% | 15.1% | 7.4× |
| 0.50 | 98.7% | 0.113% | 15.0% | 3.2× |
| 1.00 | 98.8% | 0.079% | 14.8% | 2.6× |
| 2.00 (elastic) | 98.9% | 0.062% | 14.4% | 2.3× |

Three conclusions:

1. **Coverage is robust to β (~98% throughout); only spend moves** (~6× across the
   range). So β uncertainty is a *cost* uncertainty, not a feasibility one.
2. **The ~15% peak deficit is a hard floor**, independent of β — it is a timing
   (τ) limit, not a money limit. More incentive does not close it; only a
   faster-responding sink or a pre-built buffer would.
3. Even pessimistic (inelastic) β keeps spend **under ~0.4%/yr of half-TVL**. For
   a $100M pool (half-TVL $50M), β = 1 → ~$40k/yr to hold ~98% coverage.

## Scripts

* `incentive_sim.py` — simulator, controllers (`ff`, `pi`), optimiser, β sweep.

```sh
uv run python incentive_sim.py --controller pi --optimize --save pics/incentive_pi.png
uv run python incentive_sim.py --controller pi --sweep-beta --save pics/incentive_beta_sweep.png
```
