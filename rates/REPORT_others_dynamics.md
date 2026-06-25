# Was the outflow from the other crvUSD pools incentive-driven, or pyUSD competition?

`REPORT_crvusd_aggregate.md` established that pyUSD *rush-ins* don't pull capital out of
the other crvUSD pools (rush-time anti-correlation ≈ 0). But over the months that pyUSD
was *being incentivised*, the non-pyUSD pools' own incentive fell **and** their TVL bled
out. Two candidate causes:

* **(A) incentive drop** — their CRV value roughly halved, so the endogenous-APR dead
  band says TVL must relax to a lower equilibrium; and/or
* **(B) competition** — the incentivised pyUSD pool drew capital away over its whole
  campaign (a *sustained* effect, distinct from the rush spike).

`fit_others_dynamics.py` fits the **same simplified dead-band relaxation model** chosen for
pyUSD (`REPORT_pool_dynamics.md`: fee = 0, **`p_in = 1`**, the analytically-solvable form) to
**others = aggregate − pyUSD**, driven **only by the others' own incentive** (CRV +
on-gauge YB; the non-pyUSD pYB was a voter bribe, not an LP reward, so it is already inside
CRV). The rush exponent `p_in = 1` is grafted from pyUSD; the band and τ are fit to the
peers (they are different pools — see below). The incentive-only model is therefore the
**(A)-only counterfactual** — whatever TVL it fails to explain is the candidate for (B).
The shaded bands mark when pyUSD itself was being incentivised.

(Adding the grafted rush leaves the fit essentially unchanged — R² 0.51, same band, τ_in
shifts 16→24 d to absorb it — because the aggregate operates *near* its equilibrium
edge, x ≈ 2.05, never far enough above x_hi for the rush term to bite. So the
competition result below is robust to it.)

## Result: both — incentive drop sets the trend, with a real *sustained* competition effect

![others dead-band fit](pics/others_dynamics_fit.png)

**Incentive drop is the dominant driver of the long trend.** Over Jan–Jun,
`corr(ln TVL, ln rewards) = 0.86`, elasticity `dlnL/dlnR = 0.71`, the TVL/reward ratio
stays ~constant (12→14×) and the APR multiple sits pinned at **x ≈ 2.05** (top of the
band). That is the dead-band equilibrium `L* = rewards/(x·m)` with `x` ~constant, i.e.
**L ∝ rewards** — TVL falls because the reward pot fell:

| date | others TVL | others reward | TVL/reward | x = APR/market |
|------|-----------:|--------------:|-----------:|---------------:|
| Jan 15 | $146M | $12.1M/yr | 12.1 | 2.11 |
| Mar 15 | $91M  | $6.9M/yr  | 13.3 | 2.05 |
| May 15 | $87M  | $6.4M/yr  | 13.6 | 2.05 |
| Jun 15 | $70M  | $5.0M/yr  | 13.9 | 2.04 |

**But the incentive drop alone does not explain the *full* outflow during the pyUSD
campaign — competition does the rest.** Over the 2026 pyUSD-incentivised window (Jan 31–
Apr 30) the others fell **−35%**, while the incentive-only model falls only **−22%**:
reality runs ~13 pp *below* the (A)-only counterfactual (mean residual +27%, model above
measured — top panel, black under the crimson dashed line through the shaded window).
Equivalently, the others bled out at **−35%/yr while pyUSD was incentivised vs −15%/yr
when it was not** (2026) — more than twice as fast.

**It is a sustained effect, not a rush effect.** The drain does not track pyUSD's APR
*spikes* (`corr(ΔTVL_others, APR_pyUSD − APR_others) = −0.18`, weak; rush-time
anti-correlation was ≈ 0). It tracks the *presence* of the campaign: capital leaks out
steadily for as long as pyUSD is the better-incentivised home, not in the moment of the
rush.

## Caveats

* **Partial confound.** pyUSD's incentive window overlaps the fastest part of the CRV
  decline. The dead-band model nets the CRV decline out (it *is* the incentive-only
  counterfactual), so the residual is the cleaner competition measure — but the model is
  imperfect (R² ≈ 0.51; a single sharp band holds flat then steps, while 20 pools smear
  their threshold crossings, so the model also lags). So treat "~13 pp excess outflow"
  as the right sign and rough size, not a precise split.
* Aggregate fee APR omitted (no per-pool virtual prices stored); small for all-stable
  pools, absorbed by the fitted band.

## Conclusion

The other crvUSD pools shrank **mostly because their own incentive faded** (TVL ∝ their
reward pot), **plus a genuine sustained competition effect** from the incentivised pyUSD
pool — they lose capital ~2× faster while pyUSD is being paid, beyond what their own
incentive drop justifies. The competition is **over the campaign, not at the rush**:
consistent with the earlier no-rush-rotation finding, but showing the incentives *do*
compete on the slower timescale.

## Run

```sh
uv run python fit_others_dynamics.py --save pics/others_dynamics_fit.png
```
