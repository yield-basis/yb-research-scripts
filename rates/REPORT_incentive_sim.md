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

**YB-funded buffer.** A standing buffer funded by YB tokens already absorbs net
pressure up to **~20%**, so in deployment the scheme only sees the residual
`P = max(0, net_pressure − 0.20)`. The first three sections below analyse the
scheme *without* the buffer (`--buffer 0`, the scheme handling all pressure) to
expose the mechanics and the β / derivative behaviour; the final section folds the
buffer back in.

## Result — PI controller on the worst candidate (no buffer)

`mf120_of163` (the parameter set with the largest excursions), `--buffer 0`.
Optimised PI, β = 0.5:

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

## Does a derivative term help the sharp drops? (PID)

A natural idea for the spike is a **D term** reacting to how fast pressure is
opening up (driven by price velocity), to pre-empt the move. We add it as a PID
controller (`Kd · max(0, dP/dt)`) and compare to PI at 1 h resolution (which
resolves the true 53% peak, unlike the 4 h grid above), `--buffer 0`, β = 0.5:

![PI vs PID, no buffer](pics/incentive_pid_compare_nobuf.png)

| controller | coverage | spend %/yr | peak deficit |
|------------|---------:|-----------:|-------------:|
| PI  | 95.0% | 0.108% | 21.39% |
| PID | **97.5%** | 0.114% | 21.30% |

**D buys the shoulders, not the spike.** Coverage improves +2.5 pp because the
derivative front-loads the offer at the *onset*, so the sink is already climbing
when the multi-day plateau arrives (green lifts earlier in the zoom). But the
**peak deficit barely moves** (21.4→21.3%): a derivative reacts *at* the onset —
you cannot know a crash before it starts — and with τ_in ≈ 9 d no amount of APR
fills a ~2 h spike. Proportional, integral and derivative all react at onset and
all lose the same race against τ. The instantaneous tip is irreducible by
*reacting*; only a pre-built buffer or a faster sink can cut it.

## Folding in the YB-funded buffer — cheap tail insurance

In deployment the YB-funded buffer absorbs the first 20%, so the scrvUSD scheme
only sees `P = max(0, net_pressure − 0.20)`. That residual is tiny and rare:
**mean 0.08%, peak 33%, active only 1.1% of the time** (PI vs PID, 1 h, β = 0.5):

![PI vs PID, 20% buffer](pics/incentive_pid_compare.png)

| controller | residual coverage | spend %/yr | peak deficit |
|------------|------------------:|-----------:|-------------:|
| PI  | 82.2% | **0.0059%** | 19.6% |
| PID | 83.1% | 0.0062% | 19.3% |

1. **The scheme becomes near-free insurance.** Spend collapses ~18× (0.11 →
   **0.006%/yr** of half-TVL) — it only fires during the handful of excursions
   above 20% (Jan-24, Apr-24, the Aug-24 crash, Feb-26); otherwise the buffer
   handles everything and we pay nothing.
2. **The residual is the un-catchable part by construction.** The buffer absorbs
   all the easy sustained pressure below 20%; what pokes above is the sharp tip the
   slow sink still can't chase, so "residual coverage" looks low (~83%) — that is
   not the system failing. At the worst hour the decomposition is **net pressure
   53% = 20% buffer + ~14% sink + ~19% uncovered** for a few hours.
3. **The lingering sink is free.** S stays elevated for days after the residual
   clears, but once residual hits zero the controller stops offering, so that
   crvUSD costs no incentive (it just decays with τ_out).
4. **D barely matters here** (82.2→83.1%): with the sustained shoulders removed by
   the buffer, the residual is almost entirely the instantaneous tip — nothing
   reactive catches that.

**Bottom line.** The scrvUSD incentive layer is cheap, rarely-active insurance that
extends the YB buffer's 20% reach upward. The very peak of a Black-Monday-class
crash still leaves a short (~hours) ~19%-of-half-TVL uncovered sliver that neither
the buffer nor reactive incentives close — cutting that needs a *bigger standing
buffer* or a *faster sink*, not more incentive APR.

## Scripts

* `incentive_sim.py` — simulator, controllers (`ff`, `pi`, `pid`), optimiser,
  β sweep, PI-vs-PID comparison, and the YB buffer (`--buffer`, default 0.20).

```sh
# no-buffer mechanics (scheme handles all pressure)
uv run python incentive_sim.py --controller pi --optimize --buffer 0 --save pics/incentive_pi.png
uv run python incentive_sim.py --controller pi --sweep-beta --buffer 0 --save pics/incentive_beta_sweep.png
uv run python incentive_sim.py --compare-pid --dt-hours 1 --buffer 0 --save pics/incentive_pid_compare_nobuf.png
# deployment with the 20% YB-funded buffer
uv run python incentive_sim.py --compare-pid --dt-hours 1 --save pics/incentive_pid_compare.png
```
