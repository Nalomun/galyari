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


# --- Palette -----------------------------------------------------------------
BG = "#0e1117"
PANEL = "#0e1117"
FG = "#c9d1d9"
GRID = "#2d333b"
MID = "#39d0ff"
BUY = "#3fb950"
SELL = "#f85149"
NEUTRAL = "#8b949e"


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
        self.trades: deque[dict] = deque()           # {col, price, size, side}
        self._fitted = False
        self.last_book = None

    def reset(self) -> None:
        """Clear all view history — used on a hot ticker switch."""
        self.matrix = np.zeros((self.n_rows, self.n_cols))
        self.row0_price = None
        self.mid_hist = np.full(self.n_cols, np.nan)
        self.cvd_hist = np.full(self.n_cols, np.nan)
        self.cvd = 0.0
        self.trades.clear()
        self._fitted = False
        self.last_book = None

    # --- price/row mapping ---
    def _row(self, price: float) -> int:
        return int(round((price - self.row0_price) / self.tick))

    def row_prices(self) -> np.ndarray:
        return self.row0_price + np.arange(self.n_rows) * self.tick

    def _set_center(self, mid: float) -> None:
        center = round(mid / self.tick) * self.tick
        self.row0_price = center - (self.n_rows // 2) * self.tick

    def _maybe_autofit(self, book) -> None:
        """One-time zoom: size the price band to the opening book depth + margin."""
        if self._fitted or not self.autofit or book is None:
            return
        mid = book.mid
        if mid is None:
            return
        prices = [p for p, _ in book.levels()]
        if len(prices) >= 4:
            depth = max(max(prices) - mid, mid - min(prices))
            want = int(depth / self.tick * 2.6)               # 1.3× depth each side
            self.n_rows = int(np.clip(want, 60, 400))
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

    def push(self, book, new_trades) -> None:
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

        # age existing trades one column; drop those scrolled off-screen
        for t in self.trades:
            t["col"] -= 1
        while self.trades and self.trades[0]["col"] < 0:
            self.trades.popleft()

        # ingest fresh prints at the rightmost column; integrate CVD
        for tr in new_trades:
            self.trades.append({"col": self.n_cols - 1, "price": tr.price,
                                "size": tr.size, "side": tr.side})
            self.cvd += tr.side * tr.size
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
    """Project buffered trades to (x cols, y rows, sizes, colors)."""
    xs, ys, ss, cs = [], [], [], []
    for t in hm.trades:
        r = (t["price"] - hm.row0_price) / hm.tick
        if not (0 <= r < hm.n_rows):
            continue
        xs.append(t["col"])
        ys.append(r)
        ss.append(8 + 42 * np.sqrt(t["size"] / 1000.0))      # area ∝ size
        cs.append(BUY if t["side"] > 0 else SELL if t["side"] < 0 else NEUTRAL)
    return xs, ys, ss, cs


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

    im = ax.imshow(hm.matrix, aspect="auto", origin="lower", cmap="inferno",
                   interpolation="nearest", animated=True)
    (mid_ln,) = ax.plot([], [], color=MID, lw=1.1, alpha=0.9, zorder=4)
    scat = ax.scatter([], [], s=[], c=[], edgecolors="none", alpha=0.85, zorder=5)
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

    return fig, ax, ax_cvd, im, mid_ln, scat, cvd_ln, header


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
    ap.add_argument("--record", action="store_true", help="record frames to recordings/")
    ap.add_argument("--raw", action="store_true", help="dump one book frame then continue")
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
                        control=control), daemon=True)
    t.start()

    hm = Heatmap(n_cols=args.cols, n_rows=n_rows, tick=args.tick, autofit=autofit)
    fig, ax, ax_cvd, im, mid_ln, scat, cvd_ln, header = build_figure(hm, title)
    ctx = {"symbol": args.symbol if control else None, "label": title}

    if control is not None:
        _wire_switching(fig, hm, ctx, watchlist, control)

    def update(_):
        trades = [] if args.no_trades else state.drain_trades()
        hm.push(state.get_book(), trades)
        im.set_data(hm.matrix)

        # steady color scaling: 99th percentile of nonzero cells
        nz = hm.matrix[hm.matrix > 0]
        if nz.size:
            im.set_clim(0, np.percentile(nz, 99))

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

        if np.isfinite(hm.cvd_hist).any():
            cvd_ln.set_data(x, hm.cvd_hist)
            lo, hi = np.nanmin(hm.cvd_hist), np.nanmax(hm.cvd_hist)
            pad = max(50.0, (hi - lo) * 0.1)
            ax_cvd.set_ylim(lo - pad, hi + pad)
            cvd_ln.set_color(BUY if hm.cvd >= 0 else SELL)

        header.set_text(_header_text(hm, ctx["label"]))
        return [im, mid_ln, scat, cvd_ln, header]

    _ = animation.FuncAnimation(fig, update, interval=300, blit=False,
                                cache_frame_data=False)
    try:
        plt.show()
    finally:
        if recorder is not None:
            recorder.close()


def _parse_watchlist(raw: str, symbol: str) -> list[str]:
    syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if symbol.upper() not in syms:
        syms.insert(0, symbol.upper())
    return syms


def _wire_switching(fig, hm, ctx, watchlist, control):
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


def _header_text(hm: Heatmap, title: str) -> str:
    b = hm.last_book
    if b is None or b.mid is None:
        return f"{title}   waiting for book…  (market closed / no entitlement → use --simulate)"
    imb = hm.imbalance()
    imb_s = f"{imb:+.2f}" if imb is not None else "  —"
    spr = b.spread
    spr_s = f"{spr:.2f}" if spr is not None else "—"
    return (f"{title}   mid {b.mid:.2f}   spread {spr_s}   "
            f"imbalance {imb_s}   CVD {hm.cvd:+,.0f}")


if __name__ == "__main__":
    main()
