"""Plot real Binance BTC price against a MODEL price that aggregates two inputs.

The model price at each instant is produced by `model_price(ema_binance,
chainlink)`:
  * ema_binance — an exponential moving average of the Binance spot, with a
    1/e time constant of EMA_TAU_SECONDS (continuous-time decay, discretized to
    the candle spacing as alpha = 1 - exp(-dt/tau)).
  * chainlink   — the on-chain Chainlink BTC/USD price as of that minute
    (the step series, forward-filled onto the candle grid).

`model_price` is a placeholder you replace with the real aggregation model.
It is vectorized (operates elementwise on numpy arrays), so the whole 1.08M-
point series is computed at once.

Two lines are drawn: Binance spot (real) and the model price. The dense series
use the same viewport min/max decimation + lazy redraw as
plot_chainlink_vs_price.py, so it renders instantly at any zoom.

Usage
-----
    uv run python plot_model_vs_price.py
    uv run python plot_model_vs_price.py --tau 866
    uv run python plot_model_vs_price.py --save model.png --width 2400
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

# Reuse the loaders / decimation / lazy-redraw engine from the sibling script.
from plot_chainlink_vs_price import (
    DEFAULT_CANDLES,
    DEFAULT_ORACLE,
    LazyPlot,
    load_candles,
    load_oracle,
)

# EMA 1/e time constant, in seconds. The impulse response decays to 1/e after
# this many seconds. Configurable here (override at runtime with --tau).
EMA_TAU_SECONDS = 866.0

# Chainlink feed precision (deviation threshold) for BTC/USD: 0.5%. The on-chain
# answer is only guaranteed accurate to within this band, so the model trusts
# the EMA inside the band and de-biases the Chainlink price by the band edge
# outside it.
CHAINLINK_PRECISION = 0.005


def model_price(ema_binance: np.ndarray, chainlink: np.ndarray) -> np.ndarray:
    """Aggregate the EMA of Binance spot and the current Chainlink price.

    Per timestamp, with p = CHAINLINK_PRECISION:
        chainlink > (1+p)*ema  ->  chainlink / (1+p)
        chainlink < (1-p)*ema  ->  chainlink / (1-p)
        otherwise              ->  ema
    i.e. clamp the Chainlink price back to the edge of its 0.5% precision band
    around the EMA, and fall back to the EMA when Chainlink is within the band.

    Vectorized over the candle grid (inputs/output are same-shape arrays); also
    works on scalars."""
    p = CHAINLINK_PRECISION
    ema = np.asarray(ema_binance, dtype=np.float64)
    cl = np.asarray(chainlink, dtype=np.float64)
    return np.select(
        [cl > (1 + p) * ema, cl < (1 - p) * ema],
        [cl / (1 + p), cl / (1 - p)],
        default=ema,
    )


def ema_time_constant(x: np.ndarray, dt: float, tau: float) -> np.ndarray:
    """EMA with a continuous-time 1/e constant `tau`, sampled every `dt` secs.

    alpha = 1 - exp(-dt/tau) makes the discrete EMA match the continuous decay
    exp(-t/tau), independent of the sampling interval."""
    alpha = 1.0 - np.exp(-dt / tau)
    one_minus = 1.0 - alpha
    out = np.empty_like(x)
    acc = x[0]
    for i in range(x.size):
        acc = alpha * x[i] + one_minus * acc
        out[i] = acc
    return out


def chainlink_on_grid(ts_grid: np.ndarray, ts_o: np.ndarray, px_o: np.ndarray):
    """Forward-fill the Chainlink step series onto the candle timestamps.

    Returns (chainlink_price_per_minute, n_leading_clamped). For minutes before
    the first oracle round (the ~47-min edge at the series start), there is no
    prior value, so we clamp to the first oracle price and report how many."""
    idx = np.searchsorted(ts_o, ts_grid, side="right") - 1
    n_lead = int((idx < 0).sum())
    idx = np.clip(idx, 0, ts_o.size - 1)
    return px_o[idx], n_lead


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candles", type=Path, default=DEFAULT_CANDLES)
    ap.add_argument("--oracle", type=Path, default=DEFAULT_ORACLE)
    ap.add_argument("--tau", type=float, default=EMA_TAU_SECONDS,
                    help=f"EMA 1/e time constant in seconds (default {EMA_TAU_SECONDS:g})")
    ap.add_argument("--save", type=Path, default=None,
                    help="render a static PNG to this path instead of showing")
    ap.add_argument("--width", type=int, default=2400)
    args = ap.parse_args()

    import matplotlib
    if args.save is not None:
        matplotlib.use("Agg")
    else:
        matplotlib.use("QtAgg")  # ship PyQt6; avoid GTK4Cairo auto-pick
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    epoch = mdates.date2num(np.datetime64("1970-01-01T00:00:00"))

    print(f"loading candles {args.candles.name} …", flush=True)
    ts_c, close = load_candles(args.candles)
    print(f"loading oracle  {args.oracle.name} …", flush=True)
    ts_o, px_o = load_oracle(args.oracle)

    dt = float(np.median(np.diff(ts_c)))
    print(f"candle spacing dt={dt:g}s  EMA tau={args.tau:g}s  "
          f"alpha={1.0 - np.exp(-dt / args.tau):.6f}", flush=True)

    print("computing EMA(binance) …", flush=True)
    ema_b = ema_time_constant(close, dt, args.tau)

    cl_grid, n_lead = chainlink_on_grid(ts_c, ts_o, px_o)
    if n_lead:
        print(f"note: {n_lead} leading minute(s) before first oracle round "
              f"clamped to first price {px_o[0]:,.2f}", flush=True)

    print("computing model price …", flush=True)
    model = np.asarray(model_price(ema_b, cl_grid), dtype=np.float64)

    xc = epoch + ts_c / 86400.0

    fig, ax = plt.subplots(figsize=(15, 7))
    ax.set_autoscaley_on(False)

    (line_real,) = ax.plot([], [], lw=1.2, color="steelblue", alpha=0.9,
                           label="Binance 1m close (real)")
    (line_cl,) = ax.plot([], [], lw=1.2, color="gray", alpha=0.8,
                         label="Chainlink price (on grid)")
    (line_ema,) = ax.plot([], [], lw=1.2, color="red",
                          label=f"EMA(tau={args.tau:g}s) of Binance")
    (line_model,) = ax.plot([], [], lw=1.2, color="black", zorder=4,
                            label="model price")

    ax.set_xlim(xc[0], xc[-1])
    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.set_ylabel("BTC price (USD)")
    ax.set_title(f"Real Binance price vs model price  (EMA tau={args.tau:g}s)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")

    # Reuse LazyPlot: "real" slot = Binance spot; EMA and model ride along as
    # extra dense series (all share the candle x-grid).
    lazy = LazyPlot(ax, xc, close, line_real, xo=None,
                    extra=[(cl_grid, line_cl), (ema_b, line_ema),
                           (model, line_model)])
    ax.callbacks.connect("xlim_changed", lazy.redraw)
    fig.canvas.mpl_connect("resize_event", lazy.redraw)

    fig.tight_layout()
    lazy.redraw()

    if args.save is not None:
        fig.canvas.draw()
        lazy.redraw()
        fig.savefig(args.save, dpi=args.width / 15)
        print(f"wrote {args.save}")
    else:
        print("interactive: zoom/pan re-decimates both series to the view.",
              flush=True)
        plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
