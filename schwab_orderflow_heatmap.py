#!/usr/bin/env python3
"""
Minimal Bookmap-style order-flow heatmap off the Schwab NASDAQ_BOOK stream.

Two modes:
  --simulate            synthetic order book, no Schwab needed (run this first)
  (default)             live Schwab nasdaq_book feed via schwab-py

This is a SEED, not a finished app. It solves the Schwab-specific part
(book message -> rolling time x price liquidity matrix) and renders it with
matplotlib so you can see it work in one file. Hand it to Claude Code to grow
into the real thing (trade bubbles, CVD, web/canvas renderer, recording, etc).

Deps:  pip install matplotlib numpy schwab-py
Run:   python schwab_orderflow_heatmap.py --simulate
       python schwab_orderflow_heatmap.py --symbol GOOG
"""

import argparse
import asyncio
import random
import os
import threading
import time
from collections import namedtuple

import numpy as np
import matplotlib.pyplot as plt
from matplotlib import animation

# Load .env if python-dotenv is installed (pip install python-dotenv).
# Falls back to plain os.environ / CLI args if it isn't.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- Shared latest-book state -------------------------------------------------
# Both the Schwab thread and the simulate thread write here; the matplotlib
# main loop reads it. A single dict under a lock is plenty for a demo.
Book = namedtuple("Book", ["bids", "asks", "ts"])  # bids/asks: list[(price, volume)]
_state = {"book": None}
_lock = threading.Lock()


def set_book(bids, asks):
    with _lock:
        _state["book"] = Book(bids=bids, asks=asks, ts=time.time())


def get_book():
    with _lock:
        return _state["book"]


# --- Schwab NASDAQ_BOOK parsing ----------------------------------------------
# schwab-py relabels the raw numeric fields. These constants reflect the
# documented labels; if your messages differ, run with --raw to print one
# frame and adjust the four names below. Each side is a list of price levels,
# each level carrying a TOTAL_VOLUME aggregated across market makers.
BIDS_KEY, ASKS_KEY = "BIDS", "ASKS"
BID_PRICE_KEY, ASK_PRICE_KEY = "BID_PRICE", "ASK_PRICE"
VOLUME_KEY = "TOTAL_VOLUME"


def parse_book_message(msg, dump=False):
    """schwab-py book msg -> (bids, asks) as lists of (price, volume)."""
    content = msg.get("content", [])
    if not content:
        return
    entry = content[0]
    if dump:
        import json
        print(json.dumps(entry, indent=2)[:2000])
    bids = [(lvl[BID_PRICE_KEY], lvl[VOLUME_KEY]) for lvl in entry.get(BIDS_KEY, [])]
    asks = [(lvl[ASK_PRICE_KEY], lvl[VOLUME_KEY]) for lvl in entry.get(ASKS_KEY, [])]
    if bids or asks:
        if not getattr(parse_book_message, "_seen", False):
            parse_book_message._seen = True
            print("[schwab] first book frame received \u2014 data is flowing.",
                  flush=True)
        set_book(bids, asks)


def make_stream(api_key, app_secret, callback_url, token_path, account_id):
    """Manual-flow auth on the MAIN thread (you paste the redirect URL).
    Returns a StreamClient; call run_stream() on a bg thread."""
    from schwab.auth import client_from_manual_flow
    from schwab.streaming import StreamClient

    # Manual flow: prints an auth URL, then waits for you to paste the full
    # redirect URL from the browser address bar. The redirect page will look
    # broken/unreachable (nothing is listening on the callback) -- that's fine,
    # the code is in the URL. Copy the WHOLE https://127.0.0.1:8182/?code=... string.
    client = client_from_manual_flow(
        api_key=api_key, app_secret=app_secret,
        callback_url=callback_url, token_path=token_path)
    print(f"[schwab] token written to {token_path}", flush=True)
    return StreamClient(client, account_id=account_id)


def run_stream(stream, symbol, raw=False):
    """asyncio stream loop; intended to run on a background thread."""
    async def go():
        await stream.login()
        print(f"[schwab] logged in; subscribing to NASDAQ_BOOK for {symbol} ...",
              flush=True)
        # add handler BEFORE subscribing: book data starts flowing immediately
        stream.add_nasdaq_book_handler(lambda m: parse_book_message(m, dump=raw))
        await stream.nasdaq_book_subs([symbol])
        print("[schwab] subscribed, waiting for book frames. "
              "Silence here = market closed (regular session ~9:30-16:00 ET) "
              "or no L2 entitlement.", flush=True)
        while True:
            await stream.handle_message()

    asyncio.run(go())


# --- Synthetic feed (no Schwab needed) ---------------------------------------
def run_simulate(tick=0.01, levels=40):
    mid = 100.0
    walls = {}  # price -> persistent big resting size, decays
    while True:
        mid += random.gauss(0, tick * 1.5)
        mid = round(mid / tick) * tick
        # occasionally drop a liquidity wall
        if random.random() < 0.05:
            side = random.choice([-1, 1])
            wprice = round((mid + side * random.randint(3, 15) * tick) / tick) * tick
            walls[wprice] = random.randint(3000, 12000)
        for p in list(walls):  # decay
            walls[p] *= 0.97
            if walls[p] < 200:
                del walls[p]

        def level_vol(price):
            base = random.randint(100, 1500)
            return base + int(walls.get(round(price / tick) * tick, 0))

        bids = [(round(mid - i * tick, 4), level_vol(mid - i * tick))
                for i in range(1, levels + 1)]
        asks = [(round(mid + i * tick, 4), level_vol(mid + i * tick))
                for i in range(1, levels + 1)]
        set_book(bids, asks)
        time.sleep(0.25)


# --- Rolling heatmap ----------------------------------------------------------
class Heatmap:
    """Fixed price window anchored on first mid. Recentering/auto-zoom is a
    Claude Code expansion item; for a short session this is fine."""

    def __init__(self, n_cols=240, half_levels=60, tick=0.01):
        self.n_cols = n_cols
        self.half_levels = half_levels
        self.tick = tick
        self.n_bins = 2 * half_levels + 1
        self.matrix = np.zeros((self.n_bins, n_cols))
        self.center = None
        self.bin_prices = None
        self.mid_line = np.full(n_cols, np.nan)

    def _anchor(self, mid):
        self.center = round(mid / self.tick) * self.tick
        offs = (np.arange(self.n_bins) - self.half_levels) * self.tick
        self.bin_prices = self.center + offs  # ascending price by row

    def _bin_index(self, price):
        idx = int(round((price - self.center) / self.tick)) + self.half_levels
        return idx if 0 <= idx < self.n_bins else None

    def push(self, book):
        if book is None:
            return
        mid = None
        if book.bids and book.asks:
            mid = (book.bids[0][0] + book.asks[0][0]) / 2
        if self.center is None and mid is not None:
            self._anchor(mid)
        if self.center is None:
            return
        col = np.zeros(self.n_bins)
        for price, vol in book.bids + book.asks:
            i = self._bin_index(price)
            if i is not None:
                col[i] += vol
        self.matrix = np.roll(self.matrix, -1, axis=1)
        self.matrix[:, -1] = col
        self.mid_line = np.roll(self.mid_line, -1)
        self.mid_line[-1] = mid if mid is not None else np.nan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=os.environ.get("SCHWAB_SYMBOL", "GOOG"))
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--tick", type=float, default=0.01)
    ap.add_argument("--half-levels", type=int, default=60)
    ap.add_argument("--cols", type=int, default=240)
    ap.add_argument("--raw", action="store_true", help="dump one book frame and continue")
    # Schwab creds: default to env vars (from .env), CLI args override them.
    ap.add_argument("--api-key", default=os.environ.get("SCHWAB_API_KEY"))
    ap.add_argument("--app-secret", default=os.environ.get("SCHWAB_APP_SECRET"))
    # MUST match the callback URL registered in your Schwab developer app,
    # character for character (including any trailing slash).
    ap.add_argument("--callback-url",
                    default=os.environ.get("SCHWAB_CALLBACK_URL",
                                           "https://127.0.0.1:8182/"))
    ap.add_argument("--token-path",
                    default=os.environ.get("SCHWAB_TOKEN_PATH", "token.json"))
    ap.add_argument("--account-id", type=int,
                    default=(int(os.environ["SCHWAB_ACCOUNT_ID"])
                             if os.environ.get("SCHWAB_ACCOUNT_ID") else None))
    args = ap.parse_args()

    if args.simulate:
        t = threading.Thread(target=run_simulate,
                             args=(args.tick, args.half_levels), daemon=True)
    else:
        missing = [k for k in ("api_key", "app_secret", "account_id")
                   if getattr(args, k) is None]
        if missing:
            ap.error(f"live mode needs: {', '.join(missing)} (or use --simulate)")
        # Auth happens here on the main thread so you can paste the redirect URL
        # cleanly before the plot window opens.
        stream = make_stream(args.api_key, args.app_secret, args.callback_url,
                             args.token_path, args.account_id)
        t = threading.Thread(target=run_stream,
                             args=(stream, args.symbol, args.raw), daemon=True)
    t.start()

    hm = Heatmap(n_cols=args.cols, half_levels=args.half_levels, tick=args.tick)

    fig, ax = plt.subplots(figsize=(11, 6))
    fig.canvas.manager.set_window_title(
        f"Order Flow — {'SIM' if args.simulate else args.symbol}")
    im = ax.imshow(hm.matrix, aspect="auto", origin="lower",
                   cmap="inferno", interpolation="nearest")
    (mid_ln,) = ax.plot([], [], color="cyan", lw=1.0, alpha=0.8)
    ax.set_xlabel("time \u2192")
    ax.set_ylabel("price")
    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label("resting size")

    def update(_):
        hm.push(get_book())
        im.set_data(hm.matrix)
        m = hm.matrix.max()
        if m > 0:
            im.set_clim(0, m)
        if hm.center is not None:
            ax.set_yticks(np.linspace(0, hm.n_bins - 1, 7))
            ax.set_yticklabels(
                [f"{p:.2f}" for p in np.linspace(
                    hm.bin_prices[0], hm.bin_prices[-1], 7)])
            # map mid price -> row index for the cyan line
            rows = [((p - hm.center) / hm.tick) + hm.half_levels
                    if not np.isnan(p) else np.nan for p in hm.mid_line]
            mid_ln.set_data(np.arange(hm.n_cols), rows)
        return [im, mid_ln]

    _ = animation.FuncAnimation(fig, update, interval=300, blit=False,
                                cache_frame_data=False)
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
