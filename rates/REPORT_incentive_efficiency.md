# Net efficiency of incentivising pyUSD, after peer cannibalisation

Combines the two calibrated pool models to answer one number: **how much of the TVL
the incentive parks in pyUSD is genuinely new crvUSD liquidity, vs rotated out of the
other crvUSD pools?**

* pyUSD model (`REPORT_pool_dynamics.md`) → its measured TVL `L_pyUSD(t)`.
* others model (`REPORT_others_dynamics.md`) → the peers' measured TVL `L_meas` and the
  own-incentive dead-band model `L_model` — the **no-competition counterfactual** (what
  the peers should hold on their *own* fading incentive). The peers sit **below** it
  while pyUSD is incentivised: their reward stays ~flat (−4%/yr) yet TVL drains
  −35%/yr and their APR multiple ticks **up** (x: 2.00→2.16) — capital leaving for a
  better-paid home, not an incentive-equilibrium move.

## The fit

Find the single leakage coefficient `k` that closes the gap:

    L_meas(t) + k · L_pyUSD(t)  ≈  L_model(t)        (least squares, through origin)

i.e. *what fraction of the pyUSD pool's TVL must be added back to the peers to put them
back on their own-incentive model.* That fraction is the share of pyUSD's TVL that came
out of peers.

![leakage fit](pics/incentive_efficiency.png)

**k ≈ 44%** — and it is stable across denominators:

| denominator for k | k | net efficiency (1−k) |
|---|---:|---:|
| total pyUSD TVL, full series | **44%** | **56%** |
| total pyUSD TVL, while pyUSD incentivised | 42% | 58% |
| incentive-*driven* pyUSD TVL (vs no-YB counterfactual) | 45% | 55% |

(The three agree because pyUSD barely exists without YB — its no-YB counterfactual TVL
is small — so "total" ≈ "incentive-driven" pyUSD TVL.)

## Interpretation

For roughly every **$1 of TVL the incentive puts into pyUSD, ~$0.44 rotated out of the
other crvUSD pools** and only **~$0.56 was net-new crvUSD liquidity**. So pyUSD's
incentive is about **56% efficient** at creating *new* parked crvUSD — the headline pool
APR overstates the system-wide effect by nearly a factor of two.

This matters for the supply-sink design: incentivising a crvUSD venue (scrvUSD or a
pool) to absorb net pressure should be costed at its **net** efficiency. ~40%+ of the
spend just relocates existing crvUSD between Curve pools, which does nothing for net
pressure — only the new-liquidity fraction genuinely adds absorbing capacity.

## Caveats

* The leakage is **real and material** (independently confirmed by the flat-reward /
  draining-TVL / rising-x signature above), but the **precise %** rests on the peers'
  dead-band model, which is noisy (R² ≈ 0.51) and tends to hold high then step — that
  can bias `k` somewhat **upward**. Read it as *"order ~40%, net efficiency ~half,"* not
  a sharp 44.0%.
* It is a *sustained*-campaign effect, not a rush effect — the rush brings genuinely new
  liquidity (no rotation, `REPORT_crvusd_aggregate.md`); the leakage accrues over the
  weeks the rival pool stays better-incentivised.
* pyUSD depositors are one venue's behaviour; the leakage fraction need not be identical
  for a different incentivised venue, but it is the best on-chain calibration available.

## Run

```sh
uv run python combine_efficiency.py --save pics/incentive_efficiency.png
```
