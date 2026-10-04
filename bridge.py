"""WebSocket publisher — the data layer for the browser/canvas renderer (Phase 5B).

The matplotlib renderer and this bridge are *peers*: both launch a producer thread
(sim / replay / live Schwab) that fills `state.py`, then read the same getters. The
difference is the surface — matplotlib draws locally; the bridge serializes each frame
to JSON and pushes it to any connected browser, which is the actual view. Python stays
authoritative; the browser is a pure renderer that can post control messages back
(symbol switch) through the same `feeds.StreamControl` the matplotlib UI uses.

  state.py  ──▶  bridge.py (this; asyncio WebSocket server)  ──▶  browser canvas
                 reads get_book()/drain_trades(), serializes        renders; sends
                 a frame each tick, broadcasts to all clients       {"type":"switch"}

No core changes: this imports the same `feeds`/`state`/`orderbook` modules and adds a
publisher only. Run it instead of the matplotlib renderer:

    python bridge.py --simulate
    python bridge.py --symbol AAPL --watchlist AAPL,GOOG,TSLA
    python bridge.py --replay recordings/AAPL_*.jsonl --speed 4

then open the printed http://localhost:8080 URL in a browser.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import json
import os
import threading
import time
from collections import deque

try:
    from dotenv import load_dotenv
    load_dotenv()                                    # so SCHWAB_* in .env populate os.environ
except ImportError:
    pass

import bench
import feeds
import state

try:
    import websockets
except ImportError:                                  # pragma: no cover - optional dep
    websockets = None

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
CLIENTS: set = set()                                 # connected browser sockets
# Practical ceiling on simultaneously-streamed symbols. Book data is heavy per symbol and
# Schwab caps the overall streaming rate, so a handful (4+) is the sweet spot, not dozens.
MAX_SYMBOLS = 8


# --- frame serialization -----------------------------------------------------
def _imbalance(book, depth: int = 10):
    """Top-`depth` book imbalance in [-1, 1]: (Σbid − Σask) / (Σbid + Σask)."""
    b = sum(l.volume for l in book.bids[:depth])
    a = sum(l.volume for l in book.asks[:depth])
    tot = b + a
    return (b - a) / tot if tot else None


def _display_symbols(rt, books):
    """Ordered symbols to render this tick: the subscribed list (live / multi-sim) or
    whatever has a book (single sim/replay, where the symbol isn't known up front)."""
    return rt["subscribed"] or sorted(books)


def _symbol_payload(rt, sym, book):
    """Full per-symbol state for one grid tile: book arrays, the *new* prints since the
    last frame, and server-accumulated CVD + this-tick delta (so reconnects stay
    consistent). Draining happens here, once per symbol per tick.

    Also tracks a rolling aggressor-**ambiguity** rate (Phase A/F): the fraction of recent
    prints whose side is `unknown` (printed inside the spread). CVD is only as trustworthy
    as this is low — the browser uses it to de-emphasize CVD when it spikes."""
    rt["cvd"].setdefault(sym, 0.0)
    amb = rt.setdefault("amb", {}).setdefault(sym, deque(maxlen=200))  # 1=unknown, 0=signed
    net, trs = 0.0, []
    for t in state.drain_trades(sym):
        rt["cvd"][sym] += t.side * t.size
        net += t.side * t.size
        amb.append(0 if t.side else 1)
        trs.append({"p": t.price, "s": t.size, "side": t.side})
    return {
        "bids": [[l.price, l.volume] for l in book.bids] if book is not None else [],
        "asks": [[l.price, l.volume] for l in book.asks] if book is not None else [],
        "mid": book.mid if book is not None else None,
        "spread": book.spread if book is not None else None,
        "imbalance": _imbalance(book) if book is not None else None,
        "ts": book.ts_ms if book is not None else None,
        "trades": trs, "cvd": rt["cvd"][sym], "delta": net,
        "ambiguity": (sum(amb) / len(amb)) if amb else None,
        **({"ingest": state.get_ingest_ms(sym)} if rt.get("bench") else {}),
    }


def _build_message(rt) -> str:
    """One frame carrying *every* watched symbol in full, so the browser can render a grid
    of live heatmaps (and expand any one to the full single-symbol view)."""
    books = state.get_books()
    order = _display_symbols(rt, books)
    # include any symbol that has a book even if not in `order` (defensive)
    syms = order + [s for s in books if s not in order]
    msg = {"type": "frame", "focus": state.get_focus(), "order": order,
           "symbols": {s: _symbol_payload(rt, s, books.get(s)) for s in syms}}
    if rt.get("bench"):
        msg["sent"] = time.time() * 1000.0
    return json.dumps(msg)


# --- server loops ------------------------------------------------------------
async def _push_loop(interval: float, rt: dict):
    """Drain state once per column tick and broadcast a frame to every client.

    Draining always (even with no clients) keeps per-symbol CVD continuous; the trade
    buffers self-bound via their deques, so this is cheap when nobody is watching."""
    while True:
        msg = _build_message(rt)
        if CLIENTS:
            websockets.broadcast(CLIENTS, msg)
        await asyncio.sleep(interval)


async def _handler(ws, rt: dict, control):
    """One browser connection: greet it, then consume control messages.

    A switch to an already-watched symbol is a free focus change (`state.set_focus`, no
    socket op); a new symbol is posted through `StreamControl`, which subscribes it live
    (multi) or resubscribes (single). No-op in sim/replay (no `control`)."""
    CLIENTS.add(ws)
    try:
        await ws.send(json.dumps({"type": "hello", "focus": state.get_focus() or rt["focus"],
                                  "order": rt["subscribed"], "tick": rt["tick"],
                                  "cols": rt["cols"], "live": control is not None,
                                  "multi": rt["multi"], "bench": rt.get("bench", False)}))
        async for raw in ws:
            try:
                cmd = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if cmd.get("type") == "bench" and rt.get("bench"):
                _bench_ingest(rt["bench"], cmd)
                continue
            if cmd.get("type") != "switch" or not cmd.get("symbol") or control is None:
                continue
            sym = str(cmd["symbol"]).strip().upper()
            if not sym or sym == state.get_focus():
                continue
            if sym in rt["subscribed"]:
                state.set_focus(sym)                 # instant focus change, no socket
            else:
                control.request_switch(sym)          # subscribe (multi) / resubscribe (single)
                rt["subscribed"] = (rt["subscribed"] + [sym] if rt["multi"] else [sym])
                rt["cvd"][sym] = 0.0                  # fresh CVD for a newly-shown symbol
                rt["delta"][sym] = 0.0
            rt["focus"] = sym
    finally:
        CLIENTS.discard(ws)


# --- bench (--bench) ----------------------------------------------------------
# The browser measures; the bridge aggregates and prints. Each batch the page sends is
#   {"type": "bench", "window_ms", "paints", "rafs", "render_ms": [...],
#    "lat": [[ingest→paint, ingest→sent, sent→recv, recv→paint], ...]}
# where "paint" is the first animation frame after the render that drew that book (the
# frame the canvas content is committed in). Times are wall-clock ms on one machine.
def _bench_ingest(b: dict, cmd: dict) -> None:
    try:
        b["window_ms"] += float(cmd["window_ms"])
        b["paints"] += int(cmd["paints"])
        b["rafs"] += int(cmd["rafs"])
        b["render_ms"].extend(float(x) for x in cmd["render_ms"])
        b["lat"].extend([float(v) for v in row] for row in cmd["lat"])
    except (KeyError, TypeError, ValueError):
        pass


async def _bench_watch(rt: dict, secs: float, out_path: str | None) -> None:
    """Wait for the first batch, collect for `secs`, then print the summary and return
    (which ends the bridge)."""
    b = rt["bench"]
    print("[bench] waiting for a browser — open the URL above "
          "(or: node tools/bench_browser.cjs <url> <secs>)", flush=True)
    while not b["render_ms"]:
        await asyncio.sleep(0.2)
    print(f"[bench] collecting for {secs:.0f}s ...", flush=True)
    while b["window_ms"] < secs * 1000.0:
        await asyncio.sleep(0.5)
    summ = {**b["meta"], **bench.summary(b)}
    print("[bench] " + json.dumps(summ, indent=2), flush=True)
    if out_path:
        with open(out_path, "w") as f:
            json.dump(summ, f, indent=2)
        print(f"[bench] wrote {out_path}", flush=True)


def _serve_static(host: str, port: int):
    """Tiny stdlib HTTP server for the web/ frontend, on a daemon thread, so the user
    opens one URL instead of a file:// page. Pure static; the data is all on the WS."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=WEB_DIR)
    httpd = http.server.ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# --- producer launch (mirrors the matplotlib renderer's main) ----------------
def _start_producer(args, symbols, multi):
    """Spawn the sim / replay / live producer thread and return (title, control).
    `symbols` is the resolved subscribe list; `multi` whether to run the multi-symbol path."""
    control = None
    multi_syms = symbols if multi else None
    if args.replay:
        title = f"REPLAY {os.path.basename(args.replay)}"
        t = threading.Thread(target=feeds.run_replay,
                             args=(args.replay, args.speed), daemon=True)
    elif args.simulate:
        title = "SIM"
        t = threading.Thread(
            target=feeds.run_simulate,
            kwargs=dict(tick=args.tick, with_trades=not args.no_trades,
                        symbols=multi_syms), daemon=True)
    else:
        missing = [k for k in ("api_key", "app_secret", "account_id")
                   if getattr(args, k) is None]
        if missing:
            raise SystemExit(f"live mode needs: {', '.join(missing)} (or --simulate)")
        title = symbols[0]
        control = feeds.StreamControl()
        stream = feeds.make_stream(args.api_key, args.app_secret, args.callback_url,
                                   args.token_path, args.account_id)
        t = threading.Thread(
            target=feeds.run_schwab_stream,
            kwargs=dict(stream=stream, symbol=symbols[0], symbols=multi_syms, raw=False,
                        trades=not args.no_trades, control=control), daemon=True)
    t.start()
    return title, control


async def _run(args, control, rt):
    async with websockets.serve(lambda ws: _handler(ws, rt, control),
                                args.host, args.ws_port):
        push = asyncio.create_task(_push_loop(1.0 / max(args.hz, 0.5), rt))
        if not rt.get("bench"):
            await push
            return
        await _bench_watch(rt, args.bench_secs, args.bench_out)
        push.cancel()


def main():
    ap = argparse.ArgumentParser(description="Galyari WebSocket bridge (browser renderer).")
    ap.add_argument("--symbol", default=os.environ.get("SCHWAB_SYMBOL", "GOOG"))
    ap.add_argument("--watchlist", default=os.environ.get("SCHWAB_WATCHLIST", ""))
    ap.add_argument("--simulate", action="store_true", help="synthetic feed, no creds")
    ap.add_argument("--replay", metavar="FILE", help="play back a recorded JSONL tape")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier")
    ap.add_argument("--tick", type=float, default=0.01, help="price bin size ($)")
    ap.add_argument("--cols", type=int, default=240, help="time columns (history width)")
    ap.add_argument("--hz", type=float, default=4.0, help="column/broadcast rate (Hz)")
    ap.add_argument("--no-trades", action="store_true", help="hide trades layer")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ws-port", type=int, default=8765, help="WebSocket data port")
    ap.add_argument("--web-port", type=int, default=8080, help="static frontend port")
    ap.add_argument("--bench", action="store_true",
                    help="measure render FPS, frame time and producer-to-paint latency in the "
                         "connected browser, print a summary, then exit")
    ap.add_argument("--bench-secs", type=float, default=60.0, help="bench collection window")
    ap.add_argument("--bench-out", metavar="FILE", help="also write the bench summary as JSON")
    ap.add_argument("--api-key", default=os.environ.get("SCHWAB_API_KEY"))
    ap.add_argument("--app-secret", default=os.environ.get("SCHWAB_APP_SECRET"))
    ap.add_argument("--callback-url",
                    default=os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182/"))
    ap.add_argument("--token-path", default=os.environ.get("SCHWAB_TOKEN_PATH", "token.json"))
    ap.add_argument("--account-id", type=int,
                    default=(int(os.environ["SCHWAB_ACCOUNT_ID"])
                             if os.environ.get("SCHWAB_ACCOUNT_ID") else None))
    args = ap.parse_args()

    if websockets is None:
        raise SystemExit("the bridge needs `websockets` — pip install websockets")

    raw = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]
    if args.replay:                                  # symbol comes from the tape
        subscribed, focus0 = [], "REPLAY"
    elif args.simulate:                              # sim: watchlist names ARE the symbols
        subscribed = raw
        focus0 = raw[0] if raw else "SIM"
    else:                                            # live: ensure --symbol is included
        sym0 = args.symbol.upper()
        subscribed = ([sym0] + [s for s in raw if s != sym0])[:MAX_SYMBOLS]
        if len(raw) + 1 > MAX_SYMBOLS:
            print(f"[bridge] capped at {MAX_SYMBOLS} symbols (book data rate)", flush=True)
        focus0 = sym0
    multi = len(subscribed) > 1

    title, control = _start_producer(args, subscribed, multi)
    rt = {"focus": focus0, "tick": args.tick, "cols": args.cols,
          "subscribed": subscribed if multi else ([] if not control else subscribed),
          "multi": multi, "cvd": {}, "delta": {}}
    if args.bench:
        mode = (f"replay {os.path.basename(args.replay)} @{args.speed:g}x" if args.replay
                else "simulate" if args.simulate else "live")
        rt["bench"] = {**bench.new_samples(),
                       "meta": {"renderer": "browser", "mode": mode,
                                "symbols": subscribed or [title], "hz": args.hz,
                                "cols": args.cols}}

    _serve_static(args.host, args.web_port)
    where = f"{title} [{', '.join(subscribed)}]" if multi else title
    print(f"[bridge] {where}: WS ws://{args.host}:{args.ws_port}  "
          f"·  open  http://{args.host}:{args.web_port}", flush=True)
    try:
        asyncio.run(_run(args, control, rt))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
