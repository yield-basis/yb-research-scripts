"""Plot the real BTC price (1-min candles) against the on-chain Chainlink
BTC/USD oracle on a single axis.

Two series, very different sizes:
  * candles  ~1.08M minute points  -> the real price (close)
  * oracle    ~28.7k round updates  -> Chainlink, drawn as an on-chain STEP
    (hold last answer, then jump), i.e. a staircase/sawtooth, NOT interpolated.

Why this isn't insane to render
-------------------------------
A screen is only ~2-3k pixels wide, so drawing 1.08M points is wasted work.
We keep the full arrays in memory (~17 MB) but only ever *draw* a viewport
min/max decimation: slice to the visible x-range, bucket it into ~1 bucket per
horizontal pixel, and emit the min and max of each bucket. That's ~2x the pixel
width in points (~5k) at any zoom level, and min/max keeps every visible spike,
so the curve looks identical to the full render. Zoom in -> fewer points per
bucket -> automatically sharper, down to raw points when a region is sparser
than the pixels covering it. Re-decimation runs on every zoom/pan/resize.

The oracle (28.7k) is cheap, so it is drawn in full as a `steps-post` line.

Backend: interactive (QtAgg via PyQt6) so navigation events drive the
re-decimation. Use --save to render a static PNG instead (decimated to --width).

Usage
-----
    uv run python plot_chainlink_vs_price.py
    uv run python plot_chainlink_vs_price.py --ema 240          # + EMA(240 min)
    uv run python plot_chainlink_vs_price.py --save plot.png --width 2400

Inputs default to the files in this directory:
    btcusdt-2024-F2026.json.xz   (candles)
    chainlink_btcusd_rounds.csv.xz (oracle, from fetch_chainlink.py)
"""
from __future__ import annotations

import argparse
import csv
import json
import lzma
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DEFAULT_CANDLES = HERE / "btcusdt-2024-F2026.json.xz"
DEFAULT_ORACLE = HERE / "chainlink_btcusd_rounds.csv.xz"
CACHE = HERE / "cache"

# matplotlib's default epoch is 1970-01-01, and date2num is "days since epoch",
# so a unix-seconds timestamp maps to a date number by /86400 (+ offset, =0 in
# current matplotlib, but computed for safety).


def load_candles(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (unix_seconds float64, close float64). Cached as .npz."""
    CACHE.mkdir(exist_ok=True)
    cache_f = CACHE / f"candles_{path.stem}_{int(path.stat().st_mtime)}.npz"
    if cache_f.exists():
        d = np.load(cache_f)
        return d["ts"], d["close"]
    with lzma.open(path) as fh:
        rows = json.load(fh)
    arr = np.asarray(rows, dtype=np.float64)  # [ts_ms, o, h, l, c, v]
    ts = arr[:, 0] / 1000.0
    close = arr[:, 4]
    np.savez(cache_f, ts=ts, close=close)
    return ts, close


def load_oracle(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (unix_seconds float64, price float64) sorted by time."""
    op = lzma.open(path, "rt") if path.suffix == ".xz" else open(path)
    with op as fh:
        r = csv.DictReader(fh)
        ts, px = [], []
        for row in r:
            ts.append(float(row["updated_at"]))
            px.append(float(row["price"]))
    ts = np.asarray(ts, dtype=np.float64)
    px = np.asarray(px, dtype=np.float64)
    order = np.argsort(ts, kind="stable")
    return ts[order], px[order]


def ema(x: np.ndarray, span_minutes: int) -> np.ndarray:
    """EMA over the candle close (adjust=False), candles being 1-min spaced."""
    a = 2.0 / (span_minutes + 1.0)
    out = np.empty_like(x)
    acc = x[0]
    one_minus = 1.0 - a
    for i in range(x.size):
        acc = a * x[i] + one_minus * acc
        out[i] = acc
    return out


def minmax_decimate(x: np.ndarray, y: np.ndarray, n_px: int):
    """Bucket into ~n_px buckets, emit (min,max) per bucket in x order.

    Preserves the visible envelope (spikes survive). Returns the input
    unchanged when it is already <= the point budget (so zoomed-in views are
    drawn at full fidelity)."""
    m = x.size
    budget = max(n_px, 2) * 2
    if m <= budget:
        return x, y
    edges = np.linspace(0, m, n_px + 1).astype(np.int64)
    starts = edges[:-1]
    # m > budget = 2*n_px guarantees each bucket has >= 2 points, so `starts`
    # is strictly increasing -> reduceat gives a clean per-bucket reduction.
    ymin = np.minimum.reduceat(y, starts)
    ymax = np.maximum.reduceat(y, starts)
    xs = x[starts]
    xout = np.repeat(xs, 2)
    yout = np.empty(xs.size * 2, dtype=y.dtype)
    yout[0::2] = ymin
    yout[1::2] = ymax
    return xout, yout


class LazyPlot:
    """Re-decimates the dense series to the current viewport on every nav event."""

    def __init__(self, ax, xc, close, line_real, ema_arr=None, line_ema=None,
                 xo=None):
        self.ax = ax
        self.xc = xc            # candle x (date nums), sorted
        self.close = close
        self.line_real = line_real
        self.ema_arr = ema_arr
        self.line_ema = line_ema
        self.xo = xo            # oracle x (for y-rescale to viewport)
        self.yo = None
        self._busy = False

    def redraw(self, *_):
        if self._busy:
            return
        self._busy = True
        try:
            ax = self.ax
            x0, x1 = ax.get_xlim()
            try:
                n_px = max(int(ax.get_window_extent().width), 800)
            except Exception:
                n_px = 2000

            i0 = np.searchsorted(self.xc, x0, "left")
            i1 = np.searchsorted(self.xc, x1, "right")
            i0 = max(i0 - 1, 0)
            i1 = min(i1 + 1, self.xc.size)
            xv = self.xc[i0:i1]
            yv = self.close[i0:i1]
            xd, yd = minmax_decimate(xv, yv, n_px)
            self.line_real.set_data(xd, yd)

            ylo, yhi = (np.inf, -np.inf)
            if yv.size:
                ylo, yhi = min(ylo, yv.min()), max(yhi, yv.max())

            if self.line_ema is not None:
                ev = self.ema_arr[i0:i1]
                exd, eyd = minmax_decimate(xv, ev, n_px)
                self.line_ema.set_data(exd, eyd)
                if ev.size:
                    ylo, yhi = min(ylo, ev.min()), max(yhi, ev.max())

            # include visible oracle in the y-rescale
            if self.xo is not None and self.yo is not None:
                j0 = np.searchsorted(self.xo, x0, "left")
                j1 = np.searchsorted(self.xo, x1, "right")
                if j1 > j0:
                    seg = self.yo[j0:j1]
                    ylo, yhi = min(ylo, seg.min()), max(yhi, seg.max())

            if np.isfinite(ylo) and np.isfinite(yhi) and yhi > ylo:
                pad = (yhi - ylo) * 0.05
                ax.set_ylim(ylo - pad, yhi + pad)

            ax.figure.canvas.draw_idle()
        finally:
            self._busy = False


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candles", type=Path, default=DEFAULT_CANDLES)
    ap.add_argument("--oracle", type=Path, default=DEFAULT_ORACLE)
    ap.add_argument("--ema", type=int, default=None,
                    help="overlay EMA of the real close, span in minutes")
    ap.add_argument("--save", type=Path, default=None,
                    help="render a static PNG to this path instead of showing")
    ap.add_argument("--width", type=int, default=2400,
                    help="decimation pixel width for --save (default 2400)")
    args = ap.parse_args()

    import matplotlib
    if args.save is not None:
        matplotlib.use("Agg")
    else:
        # We ship PyQt6, so pin QtAgg explicitly. Otherwise matplotlib's auto
        # backend selection may land on GTK4Cairo (needs pycairo, not a dep)
        # and fail with an ImportError.
        matplotlib.use("QtAgg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    epoch = mdates.date2num(np.datetime64("1970-01-01T00:00:00"))

    print(f"loading candles {args.candles.name} …", flush=True)
    ts_c, close = load_candles(args.candles)
    print(f"loading oracle  {args.oracle.name} …", flush=True)
    ts_o, px_o = load_oracle(args.oracle)
    print(f"candles: {ts_c.size:,}   oracle: {ts_o.size:,}", flush=True)

    xc = epoch + ts_c / 86400.0
    xo = epoch + ts_o / 86400.0

    ema_arr = None
    if args.ema:
        print(f"computing EMA(span={args.ema} min) …", flush=True)
        ema_arr = ema(close, args.ema)

    fig, ax = plt.subplots(figsize=(15, 7))
    ax.set_autoscaley_on(False)

    # Dense real price: thin line, viewport-decimated.
    (line_real,) = ax.plot([], [], lw=0.6, color="steelblue", alpha=0.9,
                           label="BTC/USDT 1m close (real)")
    line_ema = None
    if ema_arr is not None:
        (line_ema,) = ax.plot([], [], lw=1.0, color="seagreen",
                              label=f"EMA({args.ema}m) of real")

    # Oracle: full, on-chain step (hold then jump).
    ax.step(xo, px_o, where="post", lw=1.0, color="crimson",
            label="Chainlink BTC/USD (on-chain, step)", zorder=3)

    ax.set_xlim(xc[0], xc[-1])
    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.set_ylabel("BTC price (USD)")
    ax.set_title("Real BTC price vs Chainlink BTC/USD oracle")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")

    lazy = LazyPlot(ax, xc, close, line_real, ema_arr, line_ema, xo)
    lazy.yo = px_o
    ax.callbacks.connect("xlim_changed", lazy.redraw)
    fig.canvas.mpl_connect("resize_event", lazy.redraw)

    fig.tight_layout()
    lazy.redraw()  # initial decimation

    if args.save is not None:
        # static render: force the decimation width to --width regardless of fig
        fig.canvas.draw()
        lazy.redraw()
        fig.savefig(args.save, dpi=args.width / 15)  # fig is 15in wide
        print(f"wrote {args.save}")
    else:
        print("interactive: zoom/pan re-decimates the real series to the view.",
              flush=True)
        plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
