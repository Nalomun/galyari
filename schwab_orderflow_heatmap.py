#!/usr/bin/env python3
"""Bookmap-style order-flow heatmap off the Schwab NASDAQ_BOOK stream.

Modes:
  --simulate            synthetic book + trades, no Schwab needed (start here)
  --replay FILE         play back a recorded JSONL tape
  (default)             live Schwab NASDAQ_BOOK + TIMESALE_EQUITY via schwab-py

The data layer (orderbook.py / state.py / feeds.py / recorder.py) is matplotlib-free
and reusable; this file is just the CLI + renderer. See ARCHITECTURE.md.
"""

from __future__ import annotations

import argparse
import os
import threading
from collections import deque

import numpy as np
import matplotlib.pyplot as plt
from matplotlib import animation
from matplotlib import gridspec

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import state
import feeds
import recorder as rec_mod
from microstructure import MicrostructureAnalyzer


# --- Palette (Bookmap-style: navy ground, blue heatmap, green/red flow) -------
BG = "#070b12"
PANEL = "#070b12"
FG = "#c9d6e5"
GRID = "#1b2738"
MID = "#cdd9e6"      # near-white mid line, like the reference
BUY = "#26c281"
SELL = "#ec5b56"
NEUTRAL = "#5b6b80"
ICE = "#7fd8f0"      # iceberg marker
PULL = "#ffd23f"     # pulled-wall flag
# blue resting-liquidity ramp (navy → teal → cyan → near-white)
HEAT_STOPS = ["#070b12", "#102a42", "#14496c", "#1c7096", "#3ca0c4", "#78cde6", "#cdf0fc"]


# --- Rolling heatmap with vertical recentering -------------------------------
class Heatmap:
    """Rolling price×time liquidity matrix.

    Rows are absolute-price-keyed via `row0_price` (price of row 0). When the mid
    drifts out of a central dead-zone the rows are rolled to recenter, so the
    y-axis follows price without clipping or corrupting history. Trades and the
    mid line are stored in *absolute price* and projected to rows at draw time, so
    recentering never misaligns them.
    """

    def __init__(self, n_cols=240, n_rows=120, tick=0.01, autofit=True):
        self.n_cols = n_cols
        self.tick = tick
        self.autofit = autofit
        # STORE vs VIEW: `n_rows` is the (wide) matrix height we bin *everything* into;
        # `view_rows` is the visible price band. Zooming changes only the view (a display
        # crop), so zoomed-out rows already hold history instead of being rebuilt.
        self.view_rows = n_rows
        self.n_rows = max(n_rows, 700)
        self.matrix = np.zeros((self.n_rows, n_cols))
        self.row0_price: float | None = None
        self.mid_hist = np.full(n_cols, np.nan)      # absolute mid price per column
        self.bid_hist = np.full(n_cols, np.nan)      # best bid price per column
        self.ask_hist = np.full(n_cols, np.nan)      # best ask price per column
        self.cvd_hist = np.full(n_cols, np.nan)      # cumulative volume delta per column
        self.delta_hist = np.zeros(n_cols)           # per-column net (buy-sell) volume
        self.cvd = 0.0
        self.size_ref = 100.0                        # rolling typical trade size (EMA)
        self.trades: deque[dict] = deque()           # {col, price, size, side}
        self.pull_marks: deque[dict] = deque()        # {col, price, side} — fading pull flags
        self._fitted = False
        self.last_book = None

    def reset(self) -> None:
        """Clear all view history — used on a hot ticker switch."""
        self.matrix = np.zeros((self.n_rows, self.n_cols))
        self.row0_price = None
        self.mid_hist = np.full(self.n_cols, np.nan)
        self.bid_hist = np.full(self.n_cols, np.nan)
        self.ask_hist = np.full(self.n_cols, np.nan)
        self.cvd_hist = np.full(self.n_cols, np.nan)
        self.delta_hist = np.zeros(self.n_cols)
        self.cvd = 0.0
        self.size_ref = 100.0
        self.trades.clear()
        self.pull_marks.clear()
        self._fitted = False
        self.last_book = None

    def set_zoom(self, view_rows: int) -> None:
        """Change the visible price band only — a display crop, not a rebin. Liquidity is
        binned into the wider store every frame regardless of zoom, so zooming in/out just
        reveals more/less of the *existing* history. The mid line, trades and CVD are
        price/time-keyed and unaffected."""
        self.view_rows = int(np.clip(view_rows, 30, 600))
        self.autofit = False
        if self.view_rows + 4 > self.n_rows:          # rare: grow the store to fit the view
            self._grow_store(self.view_rows * 2)

    def _grow_store(self, new_rows: int) -> None:
        new = np.zeros((new_rows, self.n_cols))
        keep = min(self.matrix.shape[0], new_rows)
        new[:keep, :] = self.matrix[:keep, :]         # row0 (and prices) preserved
        self.matrix = new
        self.n_rows = new_rows

    # --- price/row mapping ---
    def _row(self, price: float) -> int:
        return int(round((price - self.row0_price) / self.tick))

    def row_prices(self) -> np.ndarray:
        return self.row0_price + np.arange(self.n_rows) * self.tick

    def _set_center(self, mid: float) -> None:
        center = round(mid / self.tick) * self.tick
        self.row0_price = center - (self.n_rows // 2) * self.tick

    def _maybe_autofit(self, book) -> None:
        """One-time zoom centered on the touch.

        Equity books are sparse and wide (a few levels near the touch, the rest $$
        away), and how far they spread is unpredictable — so deriving the band from
        the book just zooms out and buries the action. Use a deterministic price-based
        band instead: ~0.15% of price each side, clamped to a sane $ range. Far levels
        clip off-screen; use +/- to change the zoom."""
        if self._fitted or not self.autofit or book is None:
            return
        mid = book.mid
        if mid is None:
            return
        half = float(np.clip(mid * 0.0015, 0.15, 1.00))
        self.view_rows = int(np.clip(round(2 * half / self.tick), 60, 240))
        # store ≈ ±2% of price (min 4× the view, so zoom-out always has data); cap memory
        self.n_rows = int(np.clip(round(0.04 * mid / self.tick),
                                  max(4 * self.view_rows, 700), 2400))
        self.matrix = np.zeros((self.n_rows, self.n_cols))
        self._set_center(mid)
        self._fitted = True

    def _recenter(self, mid: float) -> None:
        target = self.n_rows / 2.0
        row = (mid - self.row0_price) / self.tick
        if abs(row - target) <= self.n_rows * 0.30:           # inside dead-zone
            return
        shift = int(round(target - row))
        if shift == 0:
            return
        self.matrix = np.roll(self.matrix, shift, axis=0)
        if shift > 0:
            self.matrix[:shift, :] = 0
        else:
            self.matrix[shift:, :] = 0
        self.row0_price -= shift * self.tick

    def push(self, book, new_trades, new_pulls=()) -> None:
        """Advance one time column. Always scrolls so time stays honest."""
        self.last_book = book
        mid = book.mid if book is not None else None

        if self.row0_price is None:
            if mid is None:
                return
            self._maybe_autofit(book)
            if self.row0_price is None:               # autofit disabled → center now
                self._set_center(mid)

        if mid is not None:
            self._recenter(mid)

        # build the new rightmost column from the current book
        col = np.zeros(self.n_rows)
        if book is not None and self.row0_price is not None:
            for price, vol in book.levels():
                r = self._row(price)
                if 0 <= r < self.n_rows:
                    col[r] += vol

        self.matrix = np.roll(self.matrix, -1, axis=1)
        self.matrix[:, -1] = col

        self.mid_hist = np.roll(self.mid_hist, -1)
        self.mid_hist[-1] = mid if mid is not None else np.nan

        # best bid/ask per column — drives the spread shading + touch staircase
        bb = book.best_bid if book is not None else None
        ba = book.best_ask if book is not None else None
        self.bid_hist = np.roll(self.bid_hist, -1)
        self.ask_hist = np.roll(self.ask_hist, -1)
        self.bid_hist[-1] = bb.price if bb is not None else np.nan
        self.ask_hist[-1] = ba.price if ba is not None else np.nan

        # age existing trades + pull marks one column; drop those scrolled off-screen
        for t in self.trades:
            t["col"] -= 1
        while self.trades and self.trades[0]["col"] < 0:
            self.trades.popleft()
        for pm in self.pull_marks:
            pm["col"] -= 1
        while self.pull_marks and self.pull_marks[0]["col"] < 0:
            self.pull_marks.popleft()
        for pl in new_pulls:
            self.pull_marks.append({"col": self.n_cols - 1, "price": pl.price,
                                    "side": pl.side})

        # ingest fresh prints at the rightmost column; integrate CVD + per-column delta
        col_delta = 0.0
        for tr in new_trades:
            self.trades.append({"col": self.n_cols - 1, "price": tr.price,
                                "size": tr.size, "side": tr.side})
            self.cvd += tr.side * tr.size
            col_delta += tr.side * tr.size
            self.size_ref = max(1.0, 0.94 * self.size_ref + 0.06 * tr.size)
        self.cvd_hist = np.roll(self.cvd_hist, -1)
        self.cvd_hist[-1] = self.cvd
        self.delta_hist = np.roll(self.delta_hist, -1)
        self.delta_hist[-1] = col_delta

    # --- readouts ---
    def imbalance(self) -> float | None:
        b = self.last_book
        if b is None or not b.bids or not b.asks:
            return None
        bv = sum(l.volume for l in b.bids)
        av = sum(l.volume for l in b.asks)
        tot = bv + av
        return (bv - av) / tot if tot else None


def _trade_xyc(hm: Heatmap):
    """Project buffered trades to (x cols, y rows, sizes, colors).

    Marker area scales with size *relative to the rolling typical print* (`size_ref`),
    so a name that trades in 50-share lots still shows a usable spread between small
    and large prints instead of a wall of identical dots."""
    ref = max(hm.size_ref, 1.0)
    xs, ys, ss, cs = [], [], [], []
    for t in hm.trades:
        r = (t["price"] - hm.row0_price) / hm.tick
        if not (0 <= r < hm.n_rows):
            continue
        xs.append(t["col"])
        ys.append(r)
        ss.append(float(np.clip(48 + 78 * np.sqrt(t["size"] / ref), 42, 520)))
        cs.append(BUY if t["side"] > 0 else SELL if t["side"] < 0 else NEUTRAL)
    return xs, ys, ss, cs


def _volume_profile(hm: Heatmap):
    """Total *traded* volume per visible price row over the on-screen window, plus the
    point-of-control (row with the most volume). The heatmap shows resting intent; this
    shows where business actually happened — high-volume nodes are strong S/R."""
    vol = np.zeros(hm.n_rows)
    for t in hm.trades:
        r = int(round((t["price"] - hm.row0_price) / hm.tick))
        if 0 <= r < hm.n_rows:
            vol[r] += t["size"]
    poc = int(np.argmax(vol)) if vol.any() else None
    return vol, poc


def _absorption(hm: Heatmap, lookback: int = 18, tick_tol: int = 2):
    """Heuristic: a big one-sided burst of aggression that price barely moved against.

    Returns (direction, net) or None. direction='up' means net SELLING was absorbed
    (price held → buyers defending); 'down' means net BUYING was absorbed. Conservative
    by design — only fires when the net delta over the lookback dwarfs the typical print
    and the mid moved <= tick_tol ticks, so it doesn't spam on ordinary flow."""
    d = hm.delta_hist[-lookback:]
    net = float(d.sum())
    mh = hm.mid_hist[-lookback:]
    finite = mh[np.isfinite(mh)]
    if finite.size < 2:
        return None
    moved_ticks = abs(finite[-1] - finite[0]) / hm.tick
    if abs(net) >= 8.0 * max(hm.size_ref, 1.0) and moved_ticks <= tick_tol:
        return ("up" if net < 0 else "down"), net
    return None


def _row_of(hm: Heatmap, price: float):
    r = (price - hm.row0_price) / hm.tick
    return r if 0 <= r < hm.n_rows else None


def _wall_xyc(hm: Heatmap, walls):
    """Wall markers pinned to the right edge, area ∝ size, alpha ∝ persistence.

    Persistence is baked into the RGBA color (not a per-point alpha array, which
    conflicts with a scatter's scalar-mappable)."""
    from matplotlib.colors import to_rgba
    xs, ys, ss, cs = [], [], [], []
    for w in walls:
        r = _row_of(hm, w.price)
        if r is None:
            continue
        xs.append(hm.n_cols - 1)
        ys.append(r)
        ss.append(110 + 150 * np.sqrt(w.size / 10000.0))
        base = BUY if w.side == "bid" else SELL
        cs.append(to_rgba(base, 0.35 + 0.6 * min(w.persistence, 1.0)))
    return xs, ys, ss, cs


def _ice_xyc(hm: Heatmap, icebergs):
    xs, ys, ss = [], [], []
    for ic in icebergs:
        r = _row_of(hm, ic.price)
        if r is None:
            continue
        xs.append(hm.n_cols - 1)
        ys.append(r)
        ss.append(120 + 55 * np.sqrt(ic.executed / 5000.0))
    return xs, ys, ss


def _pull_xy(hm: Heatmap):
    xs, ys = [], []
    for pm in hm.pull_marks:
        r = _row_of(hm, pm["price"])
        if r is None:
            continue
        xs.append(pm["col"])
        ys.append(r)
    return xs, ys


VP = "#8ab4f8"       # volume-profile fill (steel blue — distinct from buy/sell)
POC = "#ffd23f"      # point-of-control marker


def build_figure(hm: Heatmap, title: str):
    import matplotlib.patheffects as pe
    from matplotlib.colors import PowerNorm

    plt.rcParams.update({
        "figure.facecolor": BG, "axes.facecolor": PANEL,
        "text.color": FG, "axes.labelcolor": FG,
        "xtick.color": FG, "ytick.color": FG,
        "axes.edgecolor": GRID, "font.family": "monospace",
    })
    fig = plt.figure(figsize=(13, 7.4))
    fig.canvas.manager.set_window_title(f"Galyari — {title}")
    # 3 rows (heatmap / delta strip / CVD) × 3 cols (main / volume-profile / colorbar).
    gs = gridspec.GridSpec(3, 3, height_ratios=[4, 0.7, 1.3],
                           width_ratios=[1.0, 0.19, 0.024],
                           hspace=0.07, wspace=0.03,
                           left=0.065, right=0.95, top=0.92, bottom=0.075)
    ax = fig.add_subplot(gs[0, 0])                       # heatmap
    ax_vp = fig.add_subplot(gs[0, 1], sharey=ax)         # volume profile (by price)
    ax_delta = fig.add_subplot(gs[1, 0], sharex=ax)      # net-delta footprint
    ax_cvd = fig.add_subplot(gs[2, 0], sharex=ax)        # cumulative volume delta
    cax = fig.add_subplot(gs[0, 2])                       # colorbar (heatmap height only)

    from matplotlib.colors import LinearSegmentedColormap
    # Bookmap-style blue ramp: navy low end → cyan/white high. PowerNorm (gamma<1) lifts
    # small resting sizes out of the dark end — real books are heavy-tailed, so a linear
    # scale hides almost everything.
    # NOTE: no animated=True. The animation runs with blit=False (full redraws),
    # and on matplotlib 3.x an animated artist is *excluded* from a normal full
    # draw — it only paints via the blit path. With blit off that means the image
    # never renders (black background) while the non-animated line/scatter do. This
    # was the "invisible heatmap" bug. animated only helps when blitting, which we
    # don't do, so leave it off.
    cmap = LinearSegmentedColormap.from_list("galyari_blue", HEAT_STOPS)
    im = ax.imshow(hm.matrix, aspect="auto", origin="lower", cmap=cmap,
                   interpolation="nearest",
                   norm=PowerNorm(gamma=0.45, vmin=0, vmax=1))
    # spread shading + bid/ask touch staircase: a faint translucent band between the
    # best bid and best ask, with thin side-colored edges, so support (bid, below) vs
    # resistance (ask, above) read at a glance and a widening spread is visible.
    (bid_ln,) = ax.plot([], [], color=BUY, lw=0.8, alpha=0.55, zorder=3)
    (ask_ln,) = ax.plot([], [], color=SELL, lw=0.8, alpha=0.55, zorder=3)
    (mid_ln,) = ax.plot([], [], color=MID, lw=1.4, alpha=0.95, zorder=4)
    # neon halo around the mid line — a soft wide stroke under the crisp line, so it
    # reads cleanly over a busy heatmap without a second artist to manage.
    mid_ln.set_path_effects([pe.Stroke(linewidth=4.0, foreground=MID, alpha=0.22),
                             pe.Normal()])
    # trades: filled dot with a thin bright rim so even small prints pop off the
    # hot cells behind them. set_facecolor (not set_color) each frame keeps the rim.
    scat = ax.scatter([], [], s=[], facecolors=[], edgecolors="#0d1117",
                      linewidths=0.9, alpha=0.95, zorder=5)
    # microstructure overlays (Phase 5C): walls ◄ at right edge, icebergs ◆, pull flags ✕
    wall_scat = ax.scatter([], [], marker="<", s=[], c=[], edgecolors="#0d1117",
                           linewidths=0.8, alpha=0.95, zorder=6)
    ice_scat = ax.scatter([], [], marker="D", s=[], c=[], edgecolors=BG,
                          linewidths=0.8, alpha=0.98, zorder=8)
    pull_scat = ax.scatter([], [], marker="x", s=[], c=[], linewidths=2.2,
                           alpha=0.95, zorder=7)
    overlays = {"wall": wall_scat, "ice": ice_scat, "pull": pull_scat}
    ax.set_ylabel("price")
    ax.tick_params(labelbottom=False)
    ax.grid(True, axis="y", color="#3a4d66", lw=0.6, alpha=0.55)

    header = ax.text(0.008, 1.02, "", transform=ax.transAxes, va="bottom",
                     ha="left", fontsize=10.5, color=FG)

    # --- volume profile (right of the heatmap, shares the price axis) ---
    (vp_line,) = ax_vp.plot([], [], color=VP, lw=0.9, alpha=0.9)
    (vp_poc,) = ax_vp.plot([], [], color=POC, lw=1.1, alpha=0.9)   # point-of-control
    ax_vp.set_title("vol@price", color=NEUTRAL, fontsize=8.5, pad=3)
    # right-edge price axis lives on the far side of the volume-profile panel (it shares
    # the heatmap's price axis), so prices read on both the left and right of the chart.
    ax_vp.yaxis.set_label_position("right")
    ax_vp.yaxis.set_ticks_position("right")
    ax_vp.tick_params(labelleft=False, labelright=True, labelbottom=False, length=0,
                      labelsize=8, colors=FG)
    ax_vp.set_xlim(0, 1)
    for sp in ax_vp.spines.values():
        sp.set_alpha(0.3)

    # --- delta footprint (net buy-sell per time bucket) ---
    delta_bars = ax_delta.bar(np.arange(hm.n_cols), np.zeros(hm.n_cols),
                              width=1.0, align="center", color=NEUTRAL, linewidth=0)
    ax_delta.axhline(0, color=GRID, lw=0.6)
    ax_delta.set_ylabel("Δ", rotation=0, labelpad=10, va="center")
    ax_delta.tick_params(labelbottom=False)
    ax_delta.set_xlim(0, hm.n_cols - 1)

    (cvd_ln,) = ax_cvd.plot([], [], color=NEUTRAL, lw=1.2)
    ax_cvd.axhline(0, color=GRID, lw=0.6)
    ax_cvd.set_ylabel("CVD")
    ax_cvd.set_xlabel("time →")
    ax_cvd.set_xlim(0, hm.n_cols - 1)
    ax_cvd.grid(True, color="#3a4d66", lw=0.5, alpha=0.45)

    cbar = fig.colorbar(im, cax=cax)
    cbar.set_label("resting size", color=FG)
    cbar.ax.yaxis.set_tick_params(color=FG)
    plt.setp(plt.getp(cbar.ax.axes, "yticklabels"), color=FG)

    # vertical dashed time guides every 50 columns, across all stacked panels (drawn over
    # the heatmap so they stay visible on bright cells)
    for gx in range(50, hm.n_cols, 50):
        ax.axvline(gx, color=FG, lw=0.7, alpha=0.18, ls=(0, (2, 4)), zorder=1.5)
        ax_delta.axvline(gx, color=FG, lw=0.7, alpha=0.15, ls=(0, (2, 4)), zorder=0.5)
        ax_cvd.axvline(gx, color=FG, lw=0.7, alpha=0.15, ls=(0, (2, 4)), zorder=0.5)
    # clearer panel edges
    for a in (ax, ax_vp, ax_delta, ax_cvd):
        for sp in a.spines.values():
            sp.set_visible(True)
            sp.set_color("#46618a")
            sp.set_linewidth(1.1)
            sp.set_alpha(0.9)

    _draw_legend(fig)
    _silence_resize_widget_bug(fig)
    return {
        "fig": fig, "ax": ax, "ax_vp": ax_vp, "ax_delta": ax_delta, "ax_cvd": ax_cvd,
        "im": im, "mid_ln": mid_ln, "bid_ln": bid_ln, "ask_ln": ask_ln, "scat": scat,
        "cvd_ln": cvd_ln, "vp_line": vp_line, "vp_poc": vp_poc, "delta_bars": delta_bars,
        "header": header, "overlays": overlays,
        # holders for collections rebuilt each frame (fill_between can't be set_data'd)
        "holders": {"spread": None, "vp_fill": None},
    }


def _draw_legend(fig):
    """Compact key in the empty lower-right so the color/marker mapping isn't memorised."""
    rows = [
        (BUY, "● buy print (size∝vol)"),
        (SELL, "● sell print"),
        (MID, "━ mid · bid/ask touch"),
        (FG, "◄ wall  ◆ ice  ✕ pull"),
        (VP, "▌ vol @ price"),
        (POC, "━ point of control"),
    ]
    fig.text(0.79, 0.335, "legend", color=NEUTRAL, fontsize=8.5, va="center", ha="left")
    y = 0.30
    for color, label in rows:
        fig.text(0.79, y, label, color=color, fontsize=8, va="center", ha="left")
        y -= 0.032


def _silence_resize_widget_bug(fig):
    """Swallow only the known-harmless matplotlib widget bug where a ResizeEvent
    reaches a handler expecting `.inaxes` (TextBox on window resize). Everything
    else is passed through to the normal printer."""
    cb = fig.canvas.callbacks
    prev = getattr(cb, "exception_handler", None)

    def handler(exc):
        if isinstance(exc, AttributeError) and "inaxes" in str(exc):
            return
        if prev is not None:
            return prev(exc)
        raise exc

    cb.exception_handler = handler


def main():
    ap = argparse.ArgumentParser(description="Order-flow / liquidity heatmap.")
    ap.add_argument("--symbol", default=os.environ.get("SCHWAB_SYMBOL", "GOOG"))
    ap.add_argument("--watchlist", default=os.environ.get("SCHWAB_WATCHLIST", ""),
                    help="comma-separated symbols to cycle with n/p (live mode)")
    ap.add_argument("--simulate", action="store_true", help="synthetic feed, no creds")
    ap.add_argument("--replay", metavar="FILE", help="play back a recorded JSONL tape")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier")
    ap.add_argument("--tick", type=float, default=0.01, help="price bin size ($)")
    ap.add_argument("--rows", type=int, default=None,
                    help="price bins shown (default: auto price band ~0.15%% of price)")
    ap.add_argument("--cols", type=int, default=240, help="time columns (history)")
    ap.add_argument("--no-trades", action="store_true", help="hide trades layer")
    ap.add_argument("--no-micro", action="store_true",
                    help="disable wall/iceberg detection overlays (Phase 5C)")
    ap.add_argument("--record", action="store_true", help="record frames to recordings/")
    ap.add_argument("--raw", action="store_true", help="dump one book frame then continue")
    ap.add_argument("--bench", action="store_true",
                    help="headless (Agg): time update+draw at the normal 300ms cadence and "
                         "report FPS, frame time and producer-to-paint latency, then exit")
    ap.add_argument("--bench-secs", type=float, default=60.0, help="bench collection window")
    ap.add_argument("--bench-out", metavar="FILE", help="also write the bench summary as JSON")
    ap.add_argument("--diag", action="store_true",
                    help="print live book + level-one stats (sizes/levels) to diagnose")
    # creds (env from .env; CLI overrides)
    ap.add_argument("--api-key", default=os.environ.get("SCHWAB_API_KEY"))
    ap.add_argument("--app-secret", default=os.environ.get("SCHWAB_APP_SECRET"))
    ap.add_argument("--callback-url",
                    default=os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182/"))
    ap.add_argument("--token-path", default=os.environ.get("SCHWAB_TOKEN_PATH", "token.json"))
    ap.add_argument("--account-id", type=int,
                    default=(int(os.environ["SCHWAB_ACCOUNT_ID"])
                             if os.environ.get("SCHWAB_ACCOUNT_ID") else None))
    args = ap.parse_args()

    autofit = args.rows is None
    n_rows = args.rows if args.rows else 120

    recorder = None
    control = None
    watchlist: list[str] = []
    if args.replay:
        title = f"REPLAY {os.path.basename(args.replay)}"
        t = threading.Thread(target=feeds.run_replay,
                             args=(args.replay, args.speed), daemon=True)
    elif args.simulate:
        title = "SIM"
        if args.record:
            recorder = rec_mod.Recorder(rec_mod.default_path("SIM", 0))
        t = threading.Thread(
            target=feeds.run_simulate,
            kwargs=dict(tick=args.tick, with_trades=not args.no_trades,
                        recorder=recorder), daemon=True)
    else:
        missing = [k for k in ("api_key", "app_secret", "account_id")
                   if getattr(args, k) is None]
        if missing:
            ap.error(f"live mode needs: {', '.join(missing)} (or use --simulate)")
        title = args.symbol
        watchlist = _parse_watchlist(args.watchlist, args.symbol)
        control = feeds.StreamControl()
        stream = feeds.make_stream(args.api_key, args.app_secret, args.callback_url,
                                   args.token_path, args.account_id)
        if args.record:
            import time as _t
            recorder = rec_mod.Recorder(
                rec_mod.default_path(args.symbol, int(_t.time() * 1000)))
        t = threading.Thread(
            target=feeds.run_schwab_stream,
            kwargs=dict(stream=stream, symbol=args.symbol, raw=args.raw,
                        trades=not args.no_trades, recorder=recorder,
                        control=control, diag=args.diag), daemon=True)
    t.start()

    if args.bench:
        plt.switch_backend("agg")                    # headless: draw() is the full raster
    hm = Heatmap(n_cols=args.cols, n_rows=n_rows, tick=args.tick, autofit=autofit)
    ui = build_figure(hm, title)
    fig, ax, ax_vp, ax_delta, ax_cvd = (ui["fig"], ui["ax"], ui["ax_vp"],
                                        ui["ax_delta"], ui["ax_cvd"])
    im, mid_ln, bid_ln, ask_ln, scat = (ui["im"], ui["mid_ln"], ui["bid_ln"],
                                        ui["ask_ln"], ui["scat"])
    cvd_ln, vp_line, vp_poc = ui["cvd_ln"], ui["vp_line"], ui["vp_poc"]
    delta_bars, header, overlays, holders = (ui["delta_bars"], ui["header"],
                                             ui["overlays"], ui["holders"])
    ctx = {"symbol": args.symbol if control else None, "label": title,
           "micro": MicrostructureAnalyzer(tick=args.tick, window=args.cols)
           if not args.no_micro else None,
           "show_micro": not args.no_micro, "state": None}

    if control is not None:
        _wire_switching(fig, hm, ctx, watchlist, control, overlays)
    _wire_micro_toggle(fig, ctx, overlays)
    _wire_zoom(fig, hm)
    fig.text(0.99, 0.013, "+ / −  zoom     m  overlays", color=NEUTRAL,
             fontsize=8.5, ha="right", va="center")

    diag_frames = {"n": 0}

    def update(_):
        book = state.get_book()
        trades = [] if args.no_trades else state.drain_trades()

        if args.diag:
            diag_frames["n"] += 1
            if diag_frames["n"] in (40, 100) and hm.row0_price is not None:
                nzc = int((hm.matrix > 0).sum())
                lo = hm.row0_price
                hi = lo + hm.n_rows * hm.tick
                print(f"[diag] HEATMAP frame {diag_frames['n']}: view={hm.view_rows} "
                      f"store={hm.n_rows} band {lo:.2f}–{hi:.2f} | nonzero {nzc} "
                      f"({100 * nzc / hm.matrix.size:.1f}%) | matrix_max "
                      f"{hm.matrix.max():.0f} | size_ref {hm.size_ref:.0f} | "
                      f"bubbles {len(hm.trades)}", flush=True)

        micro = None
        if ctx["micro"] is not None:
            micro = ctx["micro"].update(book, trades)
            ctx["state"] = micro
            if recorder is not None:
                _record_micro_events(recorder, micro)

        hm.push(book, trades, micro.pulls if micro else ())
        im.set_data(hm.matrix)
        # The image spans the full (wide) store; the visible price band is a CROP set via
        # the y-limits — so +/- zoom just reveals more/less of already-binned history
        # rather than rebuilding. extent must cover the whole store or the row→data
        # mapping (mid line, bubbles, overlays) drifts out of alignment.
        store = hm.n_rows
        im.set_extent((-0.5, hm.n_cols - 0.5, -0.5, store - 0.5))
        # visible window: center on the latest mid (fallback: store center), view_rows tall
        if hm.row0_price is not None and np.isfinite(hm.mid_hist[-1]):
            cr = (hm.mid_hist[-1] - hm.row0_price) / hm.tick
        else:
            cr = store / 2.0
        lo, hi = cr - hm.view_rows / 2.0, cr + hm.view_rows / 2.0
        if lo < 0:
            lo, hi = 0.0, float(hm.view_rows)
        if hi > store:
            lo, hi = float(store - hm.view_rows), float(store)
        lo, hi = max(lo, 0.0), min(hi, float(store))
        ax.set_ylim(lo - 0.5, hi - 0.5)

        # color scale: vmax from the nonzero cells in the VISIBLE crop (PowerNorm keeps its
        # gamma). A persistent wall occupies its row in EVERY column, so it is well above
        # p97; the median is robust to that, so cap vmax at a small multiple of it — walls
        # still saturate to the bright end, but the typical book stays visible.
        crop = hm.matrix[int(lo):int(np.ceil(hi))]
        nz = crop[crop > 0]
        if nz.size:
            vmax = min(np.percentile(nz, 97), np.median(nz) * 6.0)
            im.set_clim(0, max(vmax, 1.0))

        x = np.arange(hm.n_cols)
        if hm.row0_price is not None:
            yt = np.linspace(lo, hi - 1, 7)
            labels = [f"{hm.row0_price + r * hm.tick:7.2f}" for r in yt]
            ax.set_yticks(yt)
            ax.set_yticklabels(labels)
            ax_vp.set_yticks(yt)                      # right-edge price axis (shared y)
            ax_vp.set_yticklabels(labels)
            def to_rows(h):
                return np.where(np.isnan(h), np.nan, (h - hm.row0_price) / hm.tick)

            mid_ln.set_data(x, to_rows(hm.mid_hist))
            bid_rows, ask_rows = to_rows(hm.bid_hist), to_rows(hm.ask_hist)
            bid_ln.set_data(x, bid_rows)
            ask_ln.set_data(x, ask_rows)
            # spread band: rebuild the fill (fill_between has no set_data)
            if holders["spread"] is not None:
                holders["spread"].remove()
            valid = np.isfinite(bid_rows) & np.isfinite(ask_rows)
            holders["spread"] = ax.fill_between(
                x, bid_rows, ask_rows, where=valid, interpolate=False,
                color=MID, alpha=0.10, zorder=2, linewidth=0)

            if not args.no_trades:
                xs, ys, ss, cs = _trade_xyc(hm)
                scat.set_offsets(np.c_[xs, ys] if xs else np.empty((0, 2)))
                scat.set_sizes(ss if ss else [])
                scat.set_facecolor(cs if cs else [])

            _draw_overlays(hm, micro, overlays, ctx["show_micro"])

            # volume profile (traded volume by price) + point-of-control
            vol, poc = _volume_profile(hm)
            yr = np.arange(hm.n_rows)
            vp_line.set_data(vol, yr)
            if holders["vp_fill"] is not None:
                holders["vp_fill"].remove()
            holders["vp_fill"] = ax_vp.fill_betweenx(yr, 0, vol, color=VP, alpha=0.30)
            ax_vp.set_xlim(0, max(vol.max(), 1.0) * 1.08)
            if poc is not None and vol[poc] > 0:
                vp_poc.set_data([0, vol[poc]], [poc, poc])
            else:
                vp_poc.set_data([], [])

        # delta footprint: net buy-sell per column, green up / red down
        dmax = float(np.max(np.abs(hm.delta_hist))) if hm.delta_hist.size else 0.0
        for rect, h in zip(delta_bars, hm.delta_hist):
            rect.set_height(h)
            rect.set_color(BUY if h > 0 else SELL if h < 0 else NEUTRAL)
        ax_delta.set_ylim(-dmax * 1.15 - 1, dmax * 1.15 + 1)

        if np.isfinite(hm.cvd_hist).any():
            cvd_ln.set_data(x, hm.cvd_hist)
            lo, hi = np.nanmin(hm.cvd_hist), np.nanmax(hm.cvd_hist)
            pad = max(50.0, (hi - lo) * 0.1)
            ax_cvd.set_ylim(lo - pad, hi + pad)
            cvd_ln.set_color(BUY if hm.cvd >= 0 else SELL)

        header.set_text(_header_text(hm, ctx["label"], micro, ctx["show_micro"]))
        return [im, mid_ln, bid_ln, ask_ln, scat, cvd_ln, vp_line, vp_poc, header,
                overlays["wall"], overlays["ice"], overlays["pull"]]

    if args.bench:
        mode = (f"replay {os.path.basename(args.replay)} @{args.speed:g}x" if args.replay
                else "simulate" if args.simulate else "live")
        _run_bench(fig, update, args, {"renderer": "matplotlib-agg", "mode": mode,
                                       "symbols": [title], "cols": args.cols})
        return

    _ = animation.FuncAnimation(fig, update, interval=300, blit=False,
                                cache_frame_data=False)
    try:
        plt.show()
    finally:
        if recorder is not None:
            recorder.close()


def _run_bench(fig, update, args, meta, period_s=0.3, warmup_s=3.0):
    """Drive `update` + a full `canvas.draw()` at the FuncAnimation cadence (300 ms) and
    time it. Latency is producer ingest → end of the draw that first shows that book."""
    import json
    import time
    import bench

    b = bench.new_samples()
    last_ing = None
    t_start = time.perf_counter()
    t_on, t_end = t_start + warmup_s, t_start + warmup_s + args.bench_secs
    next_t = t_start
    print(f"[bench] matplotlib/Agg {meta['mode']}: {args.bench_secs:.0f}s after "
          f"{warmup_s:.0f}s warm-up", flush=True)
    while True:
        t0 = time.perf_counter()
        if t0 >= t_end:
            break
        update(0)
        fig.canvas.draw()
        t1 = time.perf_counter()
        sym = state.get_focus()
        ing = state.get_ingest_ms(sym) if sym else None
        if t0 >= t_on:
            b["paints"] += 1
            b["render_ms"].append((t1 - t0) * 1000.0)
            if ing is not None and ing != last_ing:
                b["lat"].append([time.time() * 1000.0 - ing])
        last_ing = ing
        next_t += period_s
        time.sleep(max(0.0, next_t - time.perf_counter()))
    b["window_ms"] = (time.perf_counter() - t_on) * 1000.0
    summ = {**meta, **bench.summary(b)}
    print("[bench] " + json.dumps(summ, indent=2), flush=True)
    if args.bench_out:
        with open(args.bench_out, "w") as f:
            json.dump(summ, f, indent=2)


def _draw_overlays(hm, micro, overlays, show):
    """Paint wall / iceberg / pull markers, or clear them when hidden/absent."""
    wall_scat, ice_scat, pull_scat = overlays["wall"], overlays["ice"], overlays["pull"]
    if not show or micro is None:
        for s in (wall_scat, ice_scat, pull_scat):
            s.set_offsets(np.empty((0, 2)))
        return

    wx, wy, ws, wc = _wall_xyc(hm, micro.walls)
    wall_scat.set_offsets(np.c_[wx, wy] if wx else np.empty((0, 2)))
    wall_scat.set_sizes(ws if ws else [])
    wall_scat.set_color(wc if wc else [])

    ix, iy, iss = _ice_xyc(hm, micro.icebergs)
    ice_scat.set_offsets(np.c_[ix, iy] if ix else np.empty((0, 2)))
    ice_scat.set_sizes(iss if iss else [])
    ice_scat.set_color([ICE] * len(ix) if ix else [])

    px, py = _pull_xy(hm)
    pull_scat.set_offsets(np.c_[px, py] if px else np.empty((0, 2)))
    pull_scat.set_sizes([120] * len(px) if px else [])
    pull_scat.set_color([PULL] * len(px) if px else [])


def _wire_zoom(fig, hm):
    """Zoom the visible price band: +/- keys or the mouse wheel (works in every mode)."""
    def zoom_in():
        hm.set_zoom(int(hm.view_rows / 1.3)); fig.canvas.draw_idle()

    def zoom_out():
        hm.set_zoom(int(hm.view_rows * 1.3)); fig.canvas.draw_idle()

    def on_key(event):
        if event.key in ("+", "="):
            zoom_in()
        elif event.key in ("-", "_"):
            zoom_out()

    def on_scroll(event):
        # scroll up zooms out, scroll down zooms in (event.step +ve = up)
        (zoom_out if (event.step or 0) > 0 else zoom_in)()

    fig.canvas.mpl_connect("key_press_event", on_key)
    fig.canvas.mpl_connect("scroll_event", on_scroll)


def _wire_micro_toggle(fig, ctx, overlays):
    """`m` toggles the microstructure overlays on/off (when detection is enabled)."""
    if ctx["micro"] is None:
        return

    def on_key(event):
        if event.key == "m":
            ctx["show_micro"] = not ctx["show_micro"]
            if not ctx["show_micro"]:
                for s in overlays.values():
                    s.set_offsets(np.empty((0, 2)))
                fig.canvas.draw_idle()

    fig.canvas.mpl_connect("key_press_event", on_key)


def _record_micro_events(recorder, micro):
    # pulls are discrete (one frame); icebergs persist, so they'd spam the log —
    # they stay visible in the UI instead.
    for p in micro.pulls:
        recorder.write_event("pull", price=p.price, side=p.side, prev_size=p.prev_size)


def _parse_watchlist(raw: str, symbol: str) -> list[str]:
    syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if symbol.upper() not in syms:
        syms.insert(0, symbol.upper())
    return syms


def _wire_switching(fig, hm, ctx, watchlist, control, overlays=None):
    """Live hot-switch UI: n/p cycle the watchlist, a text box types any symbol."""
    from matplotlib.widgets import TextBox

    def do_switch(sym):
        sym = (sym or "").strip().upper()
        if not sym or sym == ctx["symbol"]:
            return
        if sym not in watchlist:
            watchlist.append(sym)
        # clear shared + local view and arm the stale-frame guard *before* the
        # stream actually resubscribes, so no old-symbol frame leaks through
        state.clear_for_symbol(sym)
        hm.reset()
        if ctx.get("micro") is not None:
            ctx["micro"].reset()            # analyzer state is per-symbol
        if overlays:
            for s in overlays.values():
                s.set_offsets(np.empty((0, 2)))
        ctx["symbol"], ctx["label"] = sym, sym
        try:
            fig.canvas.manager.set_window_title(f"Galyari — {sym}")
        except Exception:
            pass
        control.request_switch(sym)

    def on_key(event):
        if event.key in ("n", "p") and watchlist:
            cur = ctx["symbol"]
            i = watchlist.index(cur) if cur in watchlist else 0
            i = (i + (1 if event.key == "n" else -1)) % len(watchlist)
            do_switch(watchlist[i])

    fig.canvas.mpl_connect("key_press_event", on_key)

    fig.text(0.80, 0.963, "go to ▸", color=NEUTRAL, fontsize=9.5,
             va="center", ha="right")
    box_ax = fig.add_axes([0.815, 0.945, 0.10, 0.038])
    box_ax.set_facecolor("#161b22")
    tb = TextBox(box_ax, "", color="#161b22", hovercolor="#1f2630",
                 textalignment="center")
    tb.text_disp.set_color(FG)

    def on_submit(s):
        do_switch(s)
        tb.set_val("")
        # release the box's keyboard capture so +/- (and n/p) zoom/cycle instead of
        # typing into it; otherwise it stays in editing mode after Enter
        if hasattr(tb, "stop_typing"):
            tb.stop_typing()

    tb.on_submit(on_submit)
    fig._galyari_textbox = tb            # keep a ref so it isn't GC'd

    hint = "  ".join(watchlist[:6]) + ("  …" if len(watchlist) > 6 else "")
    fig.text(0.07, 0.013, f"n / p  cycle:  {hint}",
             color=NEUTRAL, fontsize=8.5, va="center")


def _header_text(hm: Heatmap, title: str, micro=None, show_micro=True) -> str:
    b = hm.last_book
    if b is None or b.mid is None:
        return f"{title}   waiting for book…  (market closed / no entitlement → use --simulate)"
    imb = hm.imbalance()
    imb_s = f"{imb:+.2f}" if imb is not None else "  —"
    spr = b.spread
    spr_s = f"{spr:.2f}" if spr is not None else "—"
    txt = (f"{title}   mid {b.mid:.2f}   spread {spr_s}   "
           f"imbalance {imb_s}   CVD {hm.cvd:+,.0f}")
    if micro is not None and show_micro:
        txt += (f"   ·   walls {len(micro.walls)}  "
                f"ice {len(micro.icebergs)}  pulls {len(hm.pull_marks)}")
    absorb = _absorption(hm)
    if absorb is not None:
        direction, net = absorb
        arrow = "↑" if direction == "up" else "↓"
        who = "sellers absorbed" if direction == "up" else "buyers absorbed"
        txt += f"   ·   ⚠ ABSORPTION {arrow} ({who}, Δ{net:+,.0f})"
    return txt


if __name__ == "__main__":
    main()
