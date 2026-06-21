# pyUSD/crvUSD pool — unified TVL dynamics (equilibrium rate + reaction times)

Replaces the per-campaign exponential fits (`REPORT_pool_apr_response.md`,
`REPORT_liquidity_response.md`) with a **single ODE fit against the whole staked-TVL
series**, driven by the measured reward rates. This captures behaviour the
piecewise fits cannot — in particular the **start/stop-on-refill** dynamic: when a
YB campaign ends the APR drops and TVL starts bleeding out, but if the campaign
refills before the outflow completes, the APR jumps back and the outflow simply
halts.

## Model (`fit_pool_dynamics.py`)

The LP APR is **endogenous** — it is `reward_rate / TVL`, so as TVL grows the APR
self-limits. State `L` = staked TVL ($); driver = the real reward series:

```
rewards(t)   = CRV_value(t) + YB_value(t)                 [$/yr]
APR a(t,L)   = fee_apr(t) + rewards / L
x            = a / m(t)            m = sUSDS market rate
dead band [x_lo, x_hi]:
    x > x_hi : inflow   toward L*_in  = rewards/(x_hi·m − fee),  rate 1/τ_in
    x < x_lo : outflow  toward L*_out = rewards/(x_lo·m − fee),  rate 1/τ_out
    else     : hold (LPs inert inside the band)
```

`CRV_value = crv_rate · gauge_rel_weight · CRV_price · yr` and
`YB_value = yb_rate · YB_price · yr` (gated to `timestamp < period_finish`), both
from `pool_apr.csv.xz`. **Prices are per-block, not constant** — over the window
CRV ranges 4.5× ($0.18–0.79) and YB **9.2×** ($0.08–0.71), so much of the dynamics
is reward *value* moving the equilibrium, independent of the emission rate.

**No boost term.** CRV emissions to the gauge are a fixed total split among stakers
by veCRV-boosted weight — boost only *redistributes* that fixed pot (one staker's
gain is another's loss), so the **average** CRV APR is exactly `CRV_value / TVL`
regardless of boost. A free boost multiplier railed to 1.0 and left the fit
byte-identical, confirming it carries no information here.

## Fit

![TVL dynamics fit](pics/pool_dynamics_fit.png)

Optimised against log-TVL (one ODE, whole series):

| quantity | value |
|----------|-------|
| **τ_in** (inflow) | **10.7 d** |
| **τ_out** (outflow) | **8.0 d** |
| **dead band** (equilibrium) | **[1.48×, 2.34×] market** |
| R² (log-TVL) | 0.92 |

* **Equilibrium rate:** capital flows until the (endogenous) APR is driven down to
  the top edge (~2.3× sUSDS) and bleeds out until it climbs to the bottom edge
  (~1.5×) — so the equilibrium LP APR sits inside that band, anchored to the market
  rate. (The unified fit's band runs a bit higher than the earlier instantaneous
  estimate `[1×, 1.9×]`; here the edges also set the absolute TVL levels via
  `L* = rewards/(x·m − fee)`, so they do double duty.)
* **Reaction times** match the campaign-relaxation fits (τ_in ~9–11 d, τ_out a bit
  slower here at 8 d vs the 4–5 d instantaneous estimate).

## Incentive accounting — two things that look missing but aren't

The model reads the **Curve gauge** `reward_data(YB)` only, so any YB paid through
other venues could be missing. Two were investigated:

### 1. End-April 2026 — a Votemarket campaign that went to *voters*, not LPs

A YB campaign ran Apr 16–30 2026; our gauge `reward_data(YB)` ends Apr 16, so it
looked missing. Tracing it: **Votemarket v2 campaign 1435** (Arbitrum platform
`0x8c2c5A…`), pyUSD gauge, reward token pYB (bridged YB), **800,000 YB over two
weekly periods**, hook = `IncentiveGaugeHook`.

Reading the hook source settles where the YB went: it bridges only the campaign
**`leftover`** (unspent vote-incentive) to **Merkl** for LP distribution. For
campaign 1435 the **`leftover` is 0** both periods (`totalDistributed` ≈ 800k), so
**~0 YB reached LPs directly — the entire 800k was a vote-incentive paid to veCRV
voters.**

That is *not* missing from the model, because vote-buying raised the gauge's
relative weight → more CRV emissions, which we already read per block. The proof is
in `crv_rel_weight`, which spikes **8.6×** for exactly the campaign window:

| window | crv_rel_weight |
|--------|----------------|
| Mar 20 – Apr 16 | 0.257% |
| **Apr 16 – Apr 30** | **2.225%** |
| Apr 30 – May 15 | 0.254% |

So the end-April incentive enters the model as the CRV boost, not as a YB-to-LP
stream — nothing to add.

### 2. November 2025 — a tiny direct StakeDAO reward

The StakeDAO pyUSD RewardVault (`0x0F67…`) `reward_data(YB)` shows a single 7-day
campaign: **2,527.6 YB + 1.5 CVX**, deposited **2025-11-03** by StakeDAO's
`RewardReceiver` (`0xca3e…`) and streamed to vault depositors through Nov 10. At
~$0.3–0.4/YB that is **~$1k of YB over a week** — a real direct LP reward, but
negligible for the dynamics.

## Tooling

* `fit_pool_dynamics.py` — the unified TVL-dynamics fit (`--x-lo/--x-hi` to fix the
  band; default fits all four parameters).
* `trace_yb_recipients.py` — aggregate YB Transfer recipients over a block window
  (node RPC, `fetch_multi`-batched `getLogs`, tqdm).
* `trace_votemarket_lp.py` — split a Votemarket campaign into voter vs LP shares by
  cross-checking claimers' veCRV votes (kept for reference; superseded by reading
  the `IncentiveGaugeHook` source, which gives the answer directly via `leftover`).

```sh
uv run python fit_pool_dynamics.py --save pics/pool_dynamics_fit.png
```
