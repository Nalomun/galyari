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
    """Manual-flow auth on the MAIN thread (you paste the redirect URL).

    Returns a StreamClient; run run_schwab_stream() with it on a bg thread.
    """
    from schwab.auth import client_from_manual_flow
    from schwab.streaming import StreamClient

    client = client_from_manual_flow(
        api_key=api_key, app_secret=app_secret,
        callback_url=callback_url, token_path=token_path)
    print(f"[schwab] token written to {token_path}", flush=True)
    return StreamClient(client, account_id=account_id)


def _on_book(msg, recorder=None):
    book = parse_nasdaq_book(msg)
    if book is None:
        return
    if not getattr(_on_book, "_seen", False):
        _on_book._seen = True
        print("[schwab] first book frame received — data is flowing.", flush=True)
    state.set_book(book)
    if recorder is not None:
        recorder.write_book(msg)


# Schwab's streamer has no time-of-sale service (it was dropped from the TDA
# legacy API). We derive prints from LEVEL_ONE_EQUITY: a new trade is emitted when
# the last-trade timestamp advances. level-one messages are deltas, so a field is
# only present when it changed — we cache the last-known trade per symbol.
_last_trade: dict[str, tuple[int, float, int]] = {}  # symbol -> (trade_ms, price, size)


def _on_level_one(msg, recorder=None):
    content = (msg or {}).get("content") or []
    book = state.get_book()
    for entry in content:
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


def run_schwab_stream(stream, symbol, raw=False, trades=True, recorder=None):
    """asyncio stream loop; intended to run on a background thread."""
    async def go():
        await stream.login()
        print(f"[schwab] logged in; subscribing to NASDAQ_BOOK for {symbol} ...",
              flush=True)
        stream.add_nasdaq_book_handler(
            lambda m: (_dump(m) if raw else None) or _on_book(m, recorder))
        await stream.nasdaq_book_subs([symbol])
        if trades:
            stream.add_level_one_equity_handler(lambda m: _on_level_one(m, recorder))
            await stream.level_one_equity_subs([symbol])
        print("[schwab] subscribed, waiting for frames. "
              "Silence here = market closed (regular session ~9:30-16:00 ET) "
              "or no L2 entitlement.", flush=True)
        while True:
            await stream.handle_message()

    asyncio.run(go())


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
