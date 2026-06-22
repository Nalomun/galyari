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


# --- Palette -----------------------------------------------------------------
BG = "#0e1117"
PANEL = "#0e1117"
FG = "#c9d1d9"
GRID = "#2d333b"
MID = "#39d0ff"
BUY = "#3fb950"
SELL = "#f85149"
NEUTRAL = "#8b949e"
ICE = "#58e0ff"      # iceberg marker
PULL = "#ffd23f"     # pulled-wall flag


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
        self.n_rows = n_rows
        self.tick = tick
        self.autofit = autofit
        self.matrix = np.zeros((n_rows, n_cols))
        self.row0_price: float | None = None
        self.mid_hist = np.full(n_cols, np.nan)      # absolute mid price per column
        self.cvd_hist = np.full(n_cols, np.nan)      # cumulative volume delta per column
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
        self.cvd_hist = np.full(self.n_cols, np.nan)
        self.cvd = 0.0
        self.size_ref = 100.0
        self.trades.clear()
        self.pull_marks.clear()
        self._fitted = False
        self.last_book = None

    def set_zoom(self, n_rows: int) -> None:
        """Manually set the visible price band (rows), recentering on the current
        center. Disables autofit. Liquidity history is cleared (can't rebin); the
        mid line, trades and CVD are price/time-keyed so they survive."""
        n_rows = int(np.clip(n_rows, 30, 600))
        center = (self.row0_price + (self.n_rows // 2) * self.tick
                  if self.row0_price is not None else None)
        self.n_rows = n_rows
        self.matrix = np.zeros((n_rows, self.n_cols))
        if center is not None:
            self.row0_price = center - (n_rows // 2) * self.tick
        self.autofit = False
        self._fitted = True

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
        self.n_rows = int(np.clip(round(2 * half / self.tick), 60, 240))
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

        # ingest fresh prints at the rightmost column; integrate CVD
        for tr in new_trades:
            self.trades.append({"col": self.n_cols - 1, "price": tr.price,
                                "size": tr.size, "side": tr.side})
            self.cvd += tr.side * tr.size
            self.size_ref = max(1.0, 0.94 * self.size_ref + 0.06 * tr.size)
        self.cvd_hist = np.roll(self.cvd_hist, -1)
        self.cvd_hist[-1] = self.cvd

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
        ss.append(float(np.clip(22 + 60 * np.sqrt(t["size"] / ref), 18, 420)))
        cs.append(BUY if t["side"] > 0 else SELL if t["side"] < 0 else NEUTRAL)
    return xs, ys, ss, cs


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
        ss.append(40 + 90 * np.sqrt(w.size / 10000.0))
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
        ss.append(60 + 30 * np.sqrt(ic.executed / 5000.0))
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


def build_figure(hm: Heatmap, title: str):
    plt.rcParams.update({
        "figure.facecolor": BG, "axes.facecolor": PANEL,
        "text.color": FG, "axes.labelcolor": FG,
        "xtick.color": FG, "ytick.color": FG,
        "axes.edgecolor": GRID, "font.family": "monospace",
    })
    fig = plt.figure(figsize=(12, 7))
    fig.canvas.manager.set_window_title(f"ovultor — {title}")
    gs = gridspec.GridSpec(2, 1, height_ratios=[4, 1], hspace=0.06,
                           left=0.07, right=0.99, top=0.92, bottom=0.07)
    ax = fig.add_subplot(gs[0])
    ax_cvd = fig.add_subplot(gs[1], sharex=ax)

    # PowerNorm (gamma<1) lifts small resting sizes out of inferno's near-black low
    # end — real books are heavy-tailed, so a linear scale hides almost everything.
    from matplotlib.colors import PowerNorm
    im = ax.imshow(hm.matrix, aspect="auto", origin="lower", cmap="inferno",
                   interpolation="nearest", animated=True,
                   norm=PowerNorm(gamma=0.45, vmin=0, vmax=1))
    (mid_ln,) = ax.plot([], [], color=MID, lw=1.1, alpha=0.9, zorder=4)
    scat = ax.scatter([], [], s=[], c=[], edgecolors="none", alpha=0.85, zorder=5)
    # microstructure overlays (Phase 5C): walls ◄ at right edge, icebergs ◆, pull flags ✕
    wall_scat = ax.scatter([], [], marker="<", s=[], c=[], edgecolors="none",
                           alpha=0.85, zorder=6)
    ice_scat = ax.scatter([], [], marker="D", s=[], c=[], edgecolors=BG,
                          linewidths=0.5, alpha=0.95, zorder=8)
    pull_scat = ax.scatter([], [], marker="x", s=[], c=[], linewidths=1.3,
                           alpha=0.9, zorder=7)
    overlays = {"wall": wall_scat, "ice": ice_scat, "pull": pull_scat}
    ax.set_ylabel("price")
    ax.tick_params(labelbottom=False)
    ax.grid(True, axis="y", color=GRID, lw=0.4, alpha=0.4)

    header = ax.text(0.008, 1.02, "", transform=ax.transAxes, va="bottom",
                     ha="left", fontsize=10.5, color=FG)

    (cvd_ln,) = ax_cvd.plot([], [], color=NEUTRAL, lw=1.2)
    ax_cvd.axhline(0, color=GRID, lw=0.6)
    ax_cvd.set_ylabel("CVD")
    ax_cvd.set_xlabel("time →")
    ax_cvd.set_xlim(0, hm.n_cols - 1)
    ax_cvd.grid(True, color=GRID, lw=0.4, alpha=0.4)

    cbar = fig.colorbar(im, ax=[ax, ax_cvd], pad=0.012, fraction=0.035)
    cbar.set_label("resting size", color=FG)
    cbar.ax.yaxis.set_tick_params(color=FG)
    plt.setp(plt.getp(cbar.ax.axes, "yticklabels"), color=FG)

    return fig, ax, ax_cvd, im, mid_ln, scat, cvd_ln, header, overlays


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
                    help="price bins shown (default: auto-fit to book depth)")
    ap.add_argument("--cols", type=int, default=240, help="time columns (history)")
    ap.add_argument("--no-trades", action="store_true", help="hide trades layer")
    ap.add_argument("--no-micro", action="store_true",
                    help="disable wall/iceberg detection overlays (Phase 5C)")
    ap.add_argument("--record", action="store_true", help="record frames to recordings/")
    ap.add_argument("--raw", action="store_true", help="dump one book frame then continue")
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

    hm = Heatmap(n_cols=args.cols, n_rows=n_rows, tick=args.tick, autofit=autofit)
    fig, ax, ax_cvd, im, mid_ln, scat, cvd_ln, header, overlays = build_figure(hm, title)
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
                print(f"[diag] HEATMAP frame {diag_frames['n']}: rows={hm.n_rows} "
                      f"window {lo:.2f}–{hi:.2f} | nonzero {nzc} "
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

        # color scale: high percentile of nonzero cells as vmax (PowerNorm keeps its
        # gamma). p97 over the whole matrix so a single wall doesn't crush the rest.
        nz = hm.matrix[hm.matrix > 0]
        if nz.size:
            im.set_clim(0, max(np.percentile(nz, 97), 1.0))

        x = np.arange(hm.n_cols)
        if hm.row0_price is not None:
            rp = hm.row_prices()
            yt = np.linspace(0, hm.n_rows - 1, 7)
            ax.set_yticks(yt)
            ax.set_yticklabels([f"{p:7.2f}" for p in
                                np.interp(yt, [0, hm.n_rows - 1], [rp[0], rp[-1]])])
            rows = (hm.mid_hist - hm.row0_price) / hm.tick
            mid_ln.set_data(x, np.where(np.isnan(hm.mid_hist), np.nan, rows))

            if not args.no_trades:
                xs, ys, ss, cs = _trade_xyc(hm)
                scat.set_offsets(np.c_[xs, ys] if xs else np.empty((0, 2)))
                scat.set_sizes(ss if ss else [])
                scat.set_color(cs if cs else [])

            _draw_overlays(hm, micro, overlays, ctx["show_micro"])

        if np.isfinite(hm.cvd_hist).any():
            cvd_ln.set_data(x, hm.cvd_hist)
            lo, hi = np.nanmin(hm.cvd_hist), np.nanmax(hm.cvd_hist)
            pad = max(50.0, (hi - lo) * 0.1)
            ax_cvd.set_ylim(lo - pad, hi + pad)
            cvd_ln.set_color(BUY if hm.cvd >= 0 else SELL)

        header.set_text(_header_text(hm, ctx["label"], micro, ctx["show_micro"]))
        return [im, mid_ln, scat, cvd_ln, header,
                overlays["wall"], overlays["ice"], overlays["pull"]]

    _ = animation.FuncAnimation(fig, update, interval=300, blit=False,
                                cache_frame_data=False)
    try:
        plt.show()
    finally:
        if recorder is not None:
            recorder.close()


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
    pull_scat.set_sizes([70] * len(px) if px else [])
    pull_scat.set_color([PULL] * len(px) if px else [])


def _wire_zoom(fig, hm):
    """+/- change the visible price band (works in every mode)."""
    def on_key(event):
        if event.key in ("+", "="):
            hm.set_zoom(int(hm.n_rows / 1.3))
            fig.canvas.draw_idle()
        elif event.key in ("-", "_"):
            hm.set_zoom(int(hm.n_rows * 1.3))
            fig.canvas.draw_idle()
    fig.canvas.mpl_connect("key_press_event", on_key)


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
            fig.canvas.manager.set_window_title(f"ovultor — {sym}")
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
    tb.on_submit(lambda s: (do_switch(s), tb.set_val("")))
    fig._ovultor_textbox = tb            # keep a ref so it isn't GC'd

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
    return txt


if __name__ == "__main__":
    main()
