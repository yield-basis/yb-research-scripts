# Is the pyUSD "rush" real new liquidity, or rotation between crvUSD pools?

The pyUSD TVL fit (`REPORT_pool_dynamics.md`) found a fast **rush-in** of deposits
when APR spikes (`p_in≈1`). If that rush were just crvUSD **rotating** from other
Curve crvUSD pools, it would be useless for the supply sink (no *new* crvUSD to
absorb net pressure). This checks it against the **aggregate** of all crvUSD pools.

Different crvUSD pools carry **different risk premiums**, so a single combined
dead-band fit would be apples-and-oranges — it is *not* attempted here. Instead this
is **descriptive**: the staked-TVL decomposition and the net incentives flowing to
the non-pyUSD pools, side by side.

## Method

`fetch_crvusd_pools.py` discovers every Curve **all-stablecoin stableswap** pool
containing crvUSD or scrvUSD (via the getPools API, all coins priced ~$1, TVL ≥
$50k — excludes crypto/tricrypto and junk like a POOH pool with `lp_price≈$88M`),
**20 pools**. For each block it sums, via one Multicall3:

  staked TVL = Σ gauge.totalSupply() · get_virtual_price()   ($, all coins ~$1)

i.e. the **full staked TVL** — excluding unstaked **PegKeeper** crvUSD (never in the
gauge), matching the pyUSD metric.

Incentives are shown as **VALUE ($/yr) reaching LPs** — never relative weight:

* **CRV emissions** — `gauge_relative_weight · CRV.rate() · CRV_price · yr`;
* **on-gauge YB** — `reward_data(YB)`, valued at the YB price (pyUSD's Oct/Feb
  campaigns; ≈0 for the others);
* **hook-routed StakeDAO/Votemarket YB** — pYB that actually reaches LPs.

**The crucial distinction on the pYB campaigns** (`fetch_votemarket_yb.py` →
`votemarket_yb_campaigns.csv`, `hook` column): a Votemarket campaign with
`hook == 0x0` is a plain **vote bribe paid to voters**, *not* an LP reward — its only
effect on the pool is the extra CRV it buys, which is **already counted** in the CRV
line. Only a campaign with an `IncentiveGaugeHook` routes YB to LPs. Of the 55 pYB
campaigns, **exactly one is hooked: pyUSD's end-April 1435** (800k YB, ~663k
hook-claimed to LPs). The biweekly campaigns to the three other gauges (`0x2280` =
crvUSD/frxUSD, `0x4e6b`, `0x95f0`; 18 each, ~1.4M YB each from 30-Oct-2025) all have
`hook == 0x0` → they went to **voters**. So they are **not** added to the non-pyUSD LP
incentive (only shown as a dashed reference line); double-counting them as LP rewards
would be wrong. (None of this is Merkl, and none of it shows in the gauge
`reward_data(YB)`.)

## Result: the rush is real new liquidity, NOT rotation

![decomposition + incentive value](pics/crvusd_others.png)

Three panels, all sharing the time axis: **(1)** staked TVL split into pyUSD vs
**others = aggregate − pyUSD**; **(2)** the LP-incentive *value* to the non-pyUSD pools
(CRV + on-gauge YB; with the voter-bribe pYB as a dashed reference line); **(3)** the
LP-incentive *value* to pyUSD — where the orange **hook-routed end-April StakeDAO
spike (663k YB to LPs)** and the green Oct/Feb on-gauge YB are explicit.

* **No rush-time anti-correlation.** Over the fastest pyUSD-inflow days,
  `corr(ΔpyUSD, Δothers) = +0.06` (≈0). Others do **not** drain when pyUSD rushes —
  if it were rotation this would be strongly negative.
* **Aggregate and incentives bump together.** When YB incentivises pyUSD (April), both
  pyUSD **and** the aggregate staked TVL rise. New crvUSD shows up; it isn't reshuffled.
* **The slow "others" decline is mostly *their own* fading CRV — plus a sustained
  competition effect.** The non-pyUSD LP incentive is essentially **all CRV** (on-gauge
  YB ≈ 0; their pYB was a voter bribe, not an LP reward). It falls from **~$12M/yr
  (Nov-2025) to ~$5M/yr (mid-2026)** as the CRV price roughly halved ($0.68→~$0.22), and
  the others' TVL ($146M→$70M) falls roughly in proportion (TVL ∝ reward pot). On top of
  that, a dead-band fit of the others (`REPORT_others_dynamics.md`) finds they bled
  ~2× faster *while pyUSD was being incentivised* than their incentive drop alone
  explains — a **sustained** competition effect (over the campaign, **not** at the
  rush). So: the rush brings new liquidity (no rotation), but the incentives do compete
  on the slower timescale.

## Conclusion

The rush is **new crvUSD**, not rotation — so the **single pyUSD pool is a valid
measurement** of depositor dynamics, and the pyUSD model **including the rush**
(`fit_pool_dynamics.py`, `incentive_sim_pyusd.py`) stands. The single clean pool
remains the better *calibration* (one risk premium, full incentive signal), while the
aggregate — once the pYB campaigns are correctly classified (only pyUSD's hooked
campaign reaches LPs; the rest are voter bribes already reflected in CRV) —
independently **confirms the rush is real** and that the other pools move with their
own (CRV-dominated) incentives.

## Scripts

```sh
uv run python fetch_crvusd_pools.py --start 2025-10-01 --points 1500   # -> crvusd_pools.csv.xz
uv run python fetch_votemarket_yb.py                                   # -> votemarket_yb_campaigns.csv
uv run python plot_crvusd_others.py --save pics/crvusd_others.png
```
