# Direct test: the pyUSD rush is new liquidity, not inter-pool migration

The leakage model (`REPORT_incentive_efficiency.md`, `REPORT_incentive_leakage.md`)
credits the **rush** inflow channel as ~100% new crvUSD, while the **slow** channel
carries the ~44% peer cannibalisation. That split is the whole reason the supply-sink
cost stays near ×1.22 instead of ×1.8–2.3. This is the direct evidence for it.

## Method

Re-fetched the aggregate of all 20 all-stable crvUSD pools at pyUSD's **~0.6 h**
cadence (`crvusd_pools_fine.csv.xz`, 10,000 points) so both series share the same fine
sampling — interpolating the coarse 4.2 h aggregate onto a fine grid had manufactured a
spurious short-lag negative (inflating the 4 h slope to −0.28; on matched sampling it is
~−0.09). Over a differencing window we regress, in **dollars**:

* `Δothers / ΔpyUSD` — pure migration ⇒ **−1**; new money ⇒ **0**.
* `Δaggregate / ΔpyUSD` — pure migration ⇒ **0** (total unchanged); new money ⇒ **+1**.

and isolate the **rush moments** (top-decile fastest pyUSD inflow). We also compute the
**cross-correlation function** `C(τ) = corr(ΔpyUSD(t), Δothers(t+τ))` of the one-step
(~0.6 h) increments, which would reveal any lead/lag drain (others emptying just before
or after a pyUSD move).

## Cross-correlation function: a narrow τ=0 spike, no migration lobe

`C(τ)` for Δothers is **−0.18 at τ=0** and decays to ~0 within the day
(`C(±1 day) = +0.01/+0.02`); its minimum over ±48 h is just −0.18, *at* τ=0. The
aggregate's `C(τ)` shows a matching **positive** spike at τ=0. Migration would instead
show `C(τ) ≈ −1` at small lags (every $ leaving others as it enters pyUSD) or a broad
negative trough — neither appears. The −0.18 is a narrow same-bar wiggle (intraday
microstructure), not a lead/lag flow.

## Result: no anti-correlation at ≤1-day; the aggregate rises ~1:1 with pyUSD

![rush migration test](pics/rush_migration_corr.png)

At the **rush moments**, by window:

| window | Δothers/ΔpyUSD | Δaggregate/ΔpyUSD |
|-------:|---------------:|------------------:|
| 0.6 h  | −0.29 | 0.71 |
| 2.4 h  | −0.09 | 0.91 |
| 6 h    | −0.06 | 0.94 |
| 12 h   | −0.04 | 0.96 |
| **24 h** | **−0.01** | **0.99** |

* **At 1 day the aggregate rises essentially 1:1 with pyUSD (slope 0.99)** and the
  others are flat (−0.01). A pyUSD rush is **new crvUSD entering the whole system**, not
  crvUSD relocating from peers — migration would force Δaggregate/ΔpyUSD ≈ 0 and
  Δothers/ΔpyUSD ≈ −1, the dotted reference lines the data ignores.
* The only negative is a **small sub-hour transient** (−0.29 at 0.6 h) that decays to ~0
  within a few hours — intraday rebalancing/microstructure noise, not sustained
  migration (sustained migration would *grow* with the window, not vanish).
* Beyond ~2 days the rush slopes run *above* +1 (Δagg/Δpy → 1.3, 1.6, 2.2): over
  multi-day windows a pyUSD rush coincides with broad crvUSD inflows (common market
  mode), so the aggregate rises by *more* than pyUSD alone — the opposite of rotation.
* The two campaign rush-ins (bottom panels) show it directly: pyUSD spikes while the
  peers' TVL does **not** mirror-dip.

## Conclusion

At the timescale the rush operates (hours to a day), pyUSD inflow is new crvUSD: the
all-pool aggregate moves with it nearly one-for-one and the peers don't drain. This
validates treating the rush channel as ~100% efficient in `incentive_sim_leakage.py`;
the ~44% cannibalisation is confined to the **slow**, multi-week channel
(`REPORT_others_dynamics.md`), exactly as the leakage split assumes.

## Run

```sh
uv run python fetch_crvusd_pools.py --start 2025-10-01 --points 10000 --out crvusd_pools_fine.csv.xz
uv run python rush_migration_corr.py --save pics/rush_migration_corr.png
```
