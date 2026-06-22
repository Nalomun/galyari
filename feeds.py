"""Producer threads that fill the shared state (state.py).

Three sources, one contract: each writes the latest `Book` (and, where applicable,
`Trade`s) into state.py and knows nothing about the renderer. matplotlib-free.

  - run_schwab_stream : live NASDAQ_BOOK + TIMESALE_EQUITY via schwab-py (asyncio)
  - run_simulate      : synthetic random-walk book + trades, no credentials
  - run_replay        : feed a recorded JSONL file back through the same plumbing
"""

from __future__ import annotations

import asyncio
import json
import random
import time

import state
from orderbook import (
    Book, Level, Trade, infer_side, parse_nasdaq_book,
)


# --- Live Schwab feed --------------------------------------------------------
def make_stream(api_key, app_secret, callback_url, token_path, account_id):
    """Build a StreamClient, reusing an existing token when possible.

    If `token_path` exists, load it (schwab-py auto-refreshes the 30-min access token
    from the ~7-day refresh token — no interaction). Only when the file is missing, or
    is unreadable/expired, do we fall back to the manual paste flow. Runs on the MAIN
    thread so the (rare) paste happens against a clean terminal.
    """
    import os
    from schwab.auth import client_from_token_file, client_from_manual_flow
    from schwab.streaming import StreamClient

    client = None
    if os.path.exists(token_path):
        try:
            client = client_from_token_file(
                token_path=token_path, api_key=api_key, app_secret=app_secret)
            print(f"[schwab] reusing token at {token_path} (no re-auth needed)",
                  flush=True)
        except Exception as e:                      # corrupt / unreadable token file
            print(f"[schwab] couldn't load {token_path} ({e}); re-authenticating…",
                  flush=True)

    if client is None:
        client = client_from_manual_flow(
            api_key=api_key, app_secret=app_secret,
            callback_url=callback_url, token_path=token_path)
        print(f"[schwab] token written to {token_path}", flush=True)

    return StreamClient(client, account_id=account_id)


def _on_book(msg, recorder=None, diag=False):
    book = parse_nasdaq_book(msg)
    if book is None:
        return
    if not getattr(_on_book, "_seen", False):
        _on_book._seen = True
        print("[schwab] first book frame received — data is flowing.", flush=True)
    if diag:
        _diag_book(book)
    state.set_book(book)
    if recorder is not None:
        recorder.write_book(msg)


def _diag_book(book):
    """One-shot: summarize a live book frame so we can sanity-check magnitudes."""
    if getattr(_diag_book, "_done", False):
        return
    _diag_book._done = True
    import statistics
    vols = [l.volume for l in book.bids] + [l.volume for l in book.asks]
    if not vols:
        print("[diag] book frame had no levels", flush=True)
        return
    prices = [l.price for l in book.bids] + [l.price for l in book.asks]
    print(f"[diag] BOOK {book.symbol}: {len(book.bids)} bid / {len(book.asks)} ask "
          f"levels | TOTAL_VOLUME min/med/max = "
          f"{min(vols)}/{int(statistics.median(vols))}/{max(vols)} | "
          f"price span {min(prices):.2f}–{max(prices):.2f} "
          f"(mid {book.mid:.2f}, ${max(prices) - min(prices):.2f} wide)", flush=True)


def _diag_l1(entry):
    """Print the first few level-one last-sale samples (to check LAST_SIZE units)."""
    n = getattr(_diag_l1, "_n", 0)
    if n >= 8:
        return
    ls, lp = entry.get("LAST_SIZE"), entry.get("LAST_PRICE")
    if ls is None:
        return
    _diag_l1._n = n + 1
    print(f"[diag] L1 {entry.get('key', '')}: LAST_PRICE={lp} LAST_SIZE={ls} "
          f"TRADE_TIME_MILLIS={entry.get('TRADE_TIME_MILLIS')}", flush=True)


# Schwab's streamer has no time-of-sale service (it was dropped from the TDA
# legacy API). We derive prints from LEVEL_ONE_EQUITY: a new trade is emitted when
# the last-trade timestamp advances. level-one messages are deltas, so a field is
# only present when it changed — we cache the last-known trade per symbol.
_last_trade: dict[str, tuple[int, float, int]] = {}  # symbol -> (trade_ms, price, size)


def _on_level_one(msg, recorder=None, diag=False):
    content = (msg or {}).get("content") or []
    book = state.get_book()
    for entry in content:
        if diag:
            _diag_l1(entry)
        symbol = str(entry.get("key", ""))
        prev = _last_trade.get(symbol)
        trade_ms = entry.get("TRADE_TIME_MILLIS")
        price = entry.get("LAST_PRICE")
        size = entry.get("LAST_SIZE")
        # carry forward fields absent from this delta
        if prev:
            trade_ms = trade_ms if trade_ms is not None else prev[0]
            price = price if price is not None else prev[1]
            size = size if size is not None else prev[2]
        if trade_ms is None or price is None or size is None:
            continue
        try:
            trade_ms, price, size = int(trade_ms), float(price), int(size)
        except (TypeError, ValueError):
            continue
        is_new = prev is None or trade_ms > prev[0] or (price, size) != (prev[1], prev[2])
        _last_trade[symbol] = (trade_ms, price, size)
        if prev is not None and is_new and size > 0:
            tr = Trade(symbol=symbol, ts_ms=trade_ms, price=price,
                       size=size, side=infer_side(price, book))
            state.add_trade(tr)
            if recorder is not None:
                recorder.write_trade(tr)


class StreamControl:
    """Thread-safe handle the renderer uses to retarget the live stream at runtime.

    The render loop (a different thread) calls `request_switch`; the command is
    hopped onto the stream's asyncio loop with `call_soon_threadsafe` and consumed
    inside the message loop, so all websocket I/O stays on one task (schwab-py's
    socket lock requires it). No-op until the loop has bound itself.
    """

    def __init__(self) -> None:
        self._loop = None
        self._queue = None

    def _bind(self, loop, queue) -> None:
        self._loop = loop
        self._queue = queue

    def request_switch(self, symbol: str) -> None:
        if self._loop is None or self._queue is None:
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, ("switch", symbol))


def run_schwab_stream(stream, symbol, raw=False, trades=True, recorder=None,
                      control: "StreamControl | None" = None, diag=False):
    """asyncio stream loop; intended to run on a background thread.

    Honors hot-switch commands posted via `control` between messages. A 0.5s poll
    timeout on handle_message keeps switches responsive even when the book is quiet.
    """
    async def go():
        ctrl_q: asyncio.Queue = asyncio.Queue()
        if control is not None:
            control._bind(asyncio.get_running_loop(), ctrl_q)

        try:
            await stream.login()
        except Exception as e:
            # most often: the ~7-day refresh token has expired, so the saved token
            # can no longer be refreshed. Deleting it forces a fresh manual flow.
            print(f"\n[schwab] login failed ({e}).\n"
                  "  If this persists, your refresh token likely expired (~7 days).\n"
                  "  Delete the token file and rerun to re-authenticate:\n"
                  "    rm token.json   (or $SCHWAB_TOKEN_PATH)\n", flush=True)
            raise
        state.set_active_symbol(symbol)
        print(f"[schwab] logged in; subscribing to NASDAQ_BOOK for {symbol} ...",
              flush=True)
        stream.add_nasdaq_book_handler(
            lambda m: (_dump(m) if raw else None) or _on_book(m, recorder, diag))
        stream.add_level_one_equity_handler(lambda m: _on_level_one(m, recorder, diag))
        await stream.nasdaq_book_subs([symbol])
        if trades:
            await stream.level_one_equity_subs([symbol])
        print("[schwab] subscribed, waiting for frames. "
              "Silence here = market closed (regular session ~9:30-16:00 ET) "
              "or no L2 entitlement.", flush=True)

        current = symbol
        while True:
            if not ctrl_q.empty():
                cmd, arg = await ctrl_q.get()
                if cmd == "switch" and arg and arg != current:
                    current = await _switch_symbol(stream, current, arg, trades)
                continue
            try:
                await asyncio.wait_for(stream.handle_message(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    asyncio.run(go())


async def _switch_symbol(stream, old, new, trades):
    """Unsubscribe `old`, subscribe `new`. Runs inline in the message loop (no
    concurrent socket access). State is cleared by the renderer at request time."""
    state.set_active_symbol(new)        # drop any in-flight old-symbol frames now
    _last_trade.clear()                 # reset per-symbol print dedup
    await stream.nasdaq_book_unsubs([old])
    await stream.nasdaq_book_subs([new])
    if trades:
        await stream.level_one_equity_unsubs([old])
        await stream.level_one_equity_subs([new])
    print(f"[schwab] switched {old} -> {new}", flush=True)
    return new


def _dump(msg):
    """One-shot raw frame dump (used by --raw)."""
    if getattr(_dump, "_done", False):
        return
    _dump._done = True
    content = (msg or {}).get("content") or []
    if content:
        print(json.dumps(content[0], indent=2)[:2000], flush=True)


# --- Synthetic feed (no Schwab needed) ---------------------------------------
def run_simulate(tick=0.01, levels=60, with_trades=True, recorder=None):
    """Random-walk mid with decaying liquidity walls + synthetic prints.

    Writes Book snapshots and (optionally) Trades into shared state, mirroring the
    live producer so the renderer is identical across sim / live / replay.
    """
    mid = 100.0
    walls: dict[float, float] = {}
    rng = random.Random(0xB00C)  # deterministic-ish walk; nice for demos/tests
    while True:
        mid += rng.gauss(0, tick * 1.5)
        mid = round(mid / tick) * tick
        if rng.random() < 0.05:
            side = rng.choice([-1, 1])
            wprice = round((mid + side * rng.randint(3, 15) * tick) / tick) * tick
            walls[wprice] = rng.randint(3000, 12000)
        for p in list(walls):
            walls[p] *= 0.97
            if walls[p] < 200:
                del walls[p]

        def level_vol(price):
            base = rng.randint(100, 1500)
            return base + int(walls.get(round(price / tick) * tick, 0))

        ts_ms = int(time.time() * 1000)
        bids = tuple(
            Level(price=round(mid - i * tick, 4), volume=level_vol(mid - i * tick))
            for i in range(1, levels + 1))
        asks = tuple(
            Level(price=round(mid + i * tick, 4), volume=level_vol(mid + i * tick))
            for i in range(1, levels + 1))
        book = Book(symbol="SIM", ts_ms=ts_ms, bids=bids, asks=asks)
        state.set_book(book)
        if recorder is not None:
            recorder.write_sim_book(book)

        if with_trades and rng.random() < 0.7:
            # a print near touch, side biased by which way mid just moved
            aggro = rng.choice([-1, 1])
            px = (asks[0].price if aggro > 0 else bids[0].price)
            px = round(px + aggro * rng.randint(0, 2) * tick, 4)
            size = rng.choice([100, 100, 200, 300, 500, 1000, 2500])
            t = Trade(symbol="SIM", ts_ms=ts_ms, price=px, size=size,
                      side=infer_side(px, book))
            state.add_trade(t)
            if recorder is not None:
                recorder.write_trade(t)

        time.sleep(0.25)


# --- Replay feed -------------------------------------------------------------
def run_replay(path, speed=1.0):
    """Feed a recorded JSONL event log back through shared state.

    Records are the line schema written by recorder.py:
      {"t": "book", ...raw msg...} | {"t": "sim_book", ...} | {"t": "trade", ...}
    Inter-event timing is reconstructed from each record's `ts_ms` (scaled by `speed`).
    """
    state.reset()
    prev_ts = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts_ms")
            if prev_ts is not None and ts is not None and speed > 0:
                dt = (ts - prev_ts) / 1000.0 / speed
                if 0 < dt < 5:
                    time.sleep(dt)
            if ts is not None:
                prev_ts = ts
            _apply_replay_record(rec)


def _apply_replay_record(rec):
    kind = rec.get("t")
    if kind in ("book", "sim_book"):
        if kind == "book":
            book = parse_nasdaq_book(rec.get("msg", {}))
        else:
            book = _book_from_dict(rec)
        if book is not None:
            state.set_book(book)
    elif kind == "trade":
        try:
            state.add_trade(Trade(symbol=rec.get("symbol", ""), ts_ms=rec.get("ts_ms", 0),
                                  price=float(rec["price"]), size=int(rec["size"]),
                                  side=int(rec.get("side", 0))))
        except (KeyError, TypeError, ValueError):
            pass


def _book_from_dict(rec):
    bids = tuple(Level(price=p, volume=v) for p, v in rec.get("bids", ()))
    asks = tuple(Level(price=p, volume=v) for p, v in rec.get("asks", ()))
    if not bids and not asks:
        return None
    return Book(symbol=rec.get("symbol", "SIM"), ts_ms=rec.get("ts_ms", 0),
                bids=bids, asks=asks)
