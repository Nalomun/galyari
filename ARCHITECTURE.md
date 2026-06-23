# Architecture

## One-sentence model

A **background producer thread** (Schwab stream, simulator, or replay) parses incoming
frames into book state and writes the latest snapshot into a **shared, lock-guarded
store**; the **matplotlib main loop** snapshots that store ~3×/s, bins it into a rolling
price×time matrix, and renders it.

```
                         ┌─────────────────────────────┐
  Schwab WebSocket ──┐   │  producer thread (daemon)   │
  (NASDAQ_BOOK)      ├──▶│  parse_nasdaq_book(msg)     │
  simulator ─────────┤   │  parse synthetic / replay   │──┐
  replay file ───────┘   └─────────────────────────────┘  │ writes
                                                           ▼
                                            ┌──────────────────────────┐
                                            │  shared state (state.py) │
                                            │  latest Book + trades,    │
                                            │  guarded by a Lock        │
                                            └──────────────────────────┘
                                                           │ reads (snapshot)
                                                           ▼
                              ┌────────────────────────────────────────┐
                              │  matplotlib main loop (FuncAnimation)   │
                              │  Heatmap.push() → rolling matrix        │
                              │  imshow + mid/bid/ask + bubbles +       │
                              │  volume profile + Δ strip + CVD         │
                              └────────────────────────────────────────┘
```

## Why threads (and why this split)

matplotlib must own the main thread (its event loop and the GUI backend require it).
Schwab's stream client is asyncio and blocks on `handle_message()`. The simulator sleeps
in a loop. None of those can share the GUI thread, so each producer runs on its own daemon
thread and communicates only through the shared store. The render loop never blocks on the
network; the producer never touches matplotlib. This is the **renderer/data seam**: the
producer side knows nothing about how (or whether) data is drawn.

The one signal that flows *back* (renderer → producer) is a hot ticker switch. The
renderer never calls schwab-py directly; it posts a command through `feeds.StreamControl`,
which hops it onto the stream's asyncio loop via `loop.call_soon_threadsafe`. The command
is consumed *inside* the message loop, so every websocket operation (subscribe,
unsubscribe, receive) stays on one task — required because schwab-py guards the socket
with a single `asyncio.Lock`. See "Hot ticker switching" below.

## The shared-state contract (`state.py`)

A tiny module holding the latest `Book` **per symbol** and a bounded trade buffer per
symbol behind one `threading.Lock`.

- **Writers** (producers): `set_book(book)`, `add_trade(trade)` — routed by `book.symbol`.
- **Reader** (render loop): `get_book(symbol=None)`, `drain_trades(symbol=None)` — the
  no-arg form returns the **focused** symbol, so the single-symbol matplotlib renderer is
  unchanged. Multi-symbol callers use `set_subscribed`, `set_focus`, `get_books`.
- The store keeps only the **latest** book per symbol (full snapshot — see DATA_SCHEMA) and
  a small ring of recent trades each. There is no history kept here; history lives in the
  renderer's rolling matrix and (optionally) the recording file.
- A guard (`_subscribed`: a *set*, or `None` = accept-all for sim/replay) drops frames for
  symbols that aren't being watched — this both prevents stale post-switch frames and bounds
  the tracked set. `_focus` selects which symbol the no-arg getters return. Phase 5A Tier 2
  generalized `_book`→`_books` and the single active symbol→`_subscribed`; the old
  `set_active_symbol` / `clear_for_symbol` shims keep single-symbol semantics intact.
- Everything handed across the boundary is treated as **immutable** by convention:
  producers build a fresh `Book` and hand it over; the reader never mutates what it reads.
  This keeps the lock hold-time to a single assignment / list append.

Empty book is a **valid state**, never an error (market closed / no entitlement).

## Data flow per render tick (~300 ms)

1. `get_book()` → latest snapshot (or `None`).
2. `Heatmap.push(book)`:
   - compute mid = (best bid + best ask) / 2,
   - **recenter** the price window if mid has drifted (Phase 2),
   - bin every `(price, volume)` level into the current column by absolute price,
   - roll the matrix left, write the new column on the right.
3. `drain_trades()` → recent trades placed as bubbles at their price row, sized by volume,
   colored by aggressor side. The same prints integrate **CVD** (running Σ signed size,
   bottom panel) and **per-column delta** (net buy−sell for that time bucket, the Δ strip).
4. record best bid/ask into `bid_hist`/`ask_hist` (drives the touch staircase + spread band)
   and traded-volume-by-price into the **volume profile** + point-of-control (right margin).
5. `MicrostructureAnalyzer.update(book, trades)` → walls / pulls / icebergs, drawn as
   right-edge markers + scrolling pull flags (Phase 5C; see below). The header also runs a
   conservative **absorption** check (big one-sided delta vs. ≤2 ticks of price movement).
6. `imshow.set_data`, autoscale color limits, redraw mid/bid/ask lines + y tick labels.

## Modules

| File | Role | Imports matplotlib? |
|------|------|---------------------|
| `orderbook.py` | **Stable core.** Typed `Book`/`Level`/`Trade`, `parse_nasdaq_book(msg)`, `OrderBook` state. Dependency-light; the seam research code imports. | No |
| `state.py` | Thread-safe shared store between producer and renderer. | No |
| `feeds.py` | Producers: live Schwab stream, simulator, replay reader; `StreamControl` for hot switching. | No |
| `recorder.py` | Append-only JSONL recording of raw frames. | No |
| `microstructure.py` | **Consumer of the core.** Wall / pull / iceberg detection over the `Book`/`Trade` stream. Stdlib only. | No |
| `schwab_orderflow_heatmap.py` | CLI entrypoint + matplotlib renderer (`Heatmap`). | Yes |
| `bridge.py` + `web/` | Alternative frontend (Phase 5B): WebSocket publisher + browser canvas. Reads the same `state.py` getters; needs `websockets`, not matplotlib. | No |

The rule: **nothing under "stable core / producers" imports matplotlib.** That the data
layer is renderer-agnostic is no longer hypothetical — `bridge.py` + `web/` is a second
frontend that reuses `feeds`/`state`/`orderbook` unchanged and never imports matplotlib
(see "Browser renderer" below).

## Auth (left as-is, locked)

Manual OAuth (`client_from_manual_flow`) runs on the **main thread before the window
opens**, so the redirect-URL paste happens against a clean terminal. Credentials come from
`.env` via `python-dotenv`. `token.json` (with refresh token) is written once and reused;
schwab-py refreshes it. Both files are gitignored. No migration, no rewrite — this works.

## Hot ticker switching (Phase 5A Tier 1)

Live mode can retarget the subscription at runtime without a restart:

- **Control path.** `n`/`p` (cycle a `--watchlist`) and the `go to ▸` text box call a
  `do_switch(sym)` on the render thread, which (1) clears shared state and arms the
  stale-frame guard (`state.clear_for_symbol`), (2) clears the local `Heatmap`, then
  (3) posts `("switch", sym)` through `StreamControl`.
- **Stream side.** The message loop polls its control queue between messages (a 0.5 s
  `wait_for` timeout on `handle_message` keeps switches responsive when the book is quiet),
  and on a switch runs `nasdaq_book_unsubs/subs` + `level_one_equity_unsubs/subs` *inline*
  — never concurrently with a receive.
- **Stale-frame guard.** `state.set_book`/`add_trade` drop any frame whose symbol ≠ the
  active symbol. The renderer arms the guard *before* the stream resubscribes, so a
  late book from the old symbol can't flash on the new symbol's chart. (In sim/replay the
  active symbol is `None`, so the guard is inert and everything is accepted.)

## Microstructure detection (Phase 5C)

`microstructure.py` is a pure consumer of the parsed stream — stdlib only, importable by
research code without the renderer. `MicrostructureAnalyzer.update(book, trades)` keeps a
sliding window (`window` ticks) of per-price state and returns a `MicroState`:

- **Wall** — a level that is large *now* (≥ `wall_mult`× the book's median size) **and** has
  been large for ≥ `wall_min_big_frames` frames (persistence). Requiring "big now" means a
  pulled wall stops being reported immediately.
- **Pull** — a level that was a wall last tick and whose size just collapsed (≤ `pull_drop`
  of its wall size). Discrete, one-shot; recorded to the event log when `--record`.
- **Iceberg** — a level whose cumulative *executed* volume ≫ its displayed size with
  repeated refills. Crucially a refill only counts when it follows **execution** at that
  price within `refill_exec_window` frames — that ties refills to trading and rejects
  pure book-size noise (the main false-positive source).

The renderer state lives on the main thread alongside the analyzer; per-symbol state is
reset on a hot switch (`analyzer.reset()` + cleared overlays). Detection is tuned to favor
**precision over recall** — better to miss a marginal wall than to clutter the view.

## Render panels & analytics overlays

The renderer is a **3×3 `GridSpec`**: heatmap (main) + volume profile (right, shares the
price axis) + colorbar across the top row; the **Δ footprint** strip and the **CVD** line
stack below the heatmap and share its x (time) axis. `build_figure` returns a **handle
dict**, not a positional tuple — the old 9-tuple stopped scaling once the panel count grew,
and a dict lets `update()` pull artists by name (`ui["vp_line"]`, `ui["delta_bars"]`, …).

All series are derived in the renderer from the parsed `Book`/`Trade` stream — **no core
changes**. `Heatmap` tracks three new scroll-aligned histories alongside `mid_hist`, all
rolled one column per tick so they survive zoom/recenter the same way:

- `bid_hist` / `ask_hist` — best bid/ask price per column → the touch staircase + the
  translucent spread band (`fill_between`).
- `delta_hist` — net (buy−sell) volume per column → the Δ footprint bars. Distinct from
  `cvd_hist`, which is the running cumulative sum.

Two artists can't be `set_data`'d in place because they're collections, so they're
`.remove()`d and rebuilt each frame from `holders` (the spread `fill_between` and the
volume-profile `fill_betweenx`). The **volume profile** sums `trades` by price row over the
visible window and marks the point-of-control (argmax); the **absorption** flag is a
header-only heuristic (`_absorption`) — it fires only when |Σ delta| over a short lookback
is ≥ 8× the rolling typical print *and* the mid moved ≤ 2 ticks, deliberately quiet to
avoid marker spam. A compact **legend** is drawn once in the empty lower-right.

> The "invisible heatmap" footgun lives here too: the `imshow` artist must **not** be
> `animated=True`. The animation runs `blit=False` (full redraws), and an animated artist
> is excluded from a normal full draw on matplotlib 3.x — it only paints on the blit path,
> so with blit off the heatmap never rendered (black background) while the non-animated
> lines/scatter did.

## Known limitations

- **matplotlib** is fine as a seed renderer but not ideal for a high-FPS scrolling
  heatmap; a web/canvas frontend is the eventual path (Phase 5, propose-only).
- **Single symbol** per process.
- Color uses `PowerNorm(gamma≈0.45)` with a per-frame 97th-percentile `vmax`, so small
  resting sizes stay visible on heavy-tailed, thin equity books while walls still pop.
- The visible price band is a fixed window (~0.15% of price each side) centered on the
  touch, not a fit to full book depth — equity books are sparse and wide, so fitting the
  furthest level buries the action. `+`/`−` adjust it. The image extent and axes y-limits
  are re-locked to the current row count every frame (they must track `n_rows` or the
  heatmap, mid line and bubbles fall into mismatched coordinate systems).
- The per-venue breakdown of each level is parsed and available on `Level` but not yet
  visualized.

## Browser renderer (Phase 5B)

`bridge.py` is a **peer of the matplotlib renderer**, not a layer on top of it: it launches
the same producer thread (`_start_producer` mirrors the renderer's `main`) filling
`state.py`, then runs an asyncio WebSocket server instead of a GUI loop.

```
state.py ──▶ bridge.py ─ push loop (──▶ websockets.broadcast) ──▶ browser canvas (web/)
             get_book() + drain_trades() once per --hz tick        ingest → scroll → draw
             ◀── {"type":"switch"} ── StreamControl ──◀────────── go-to box
```

- **One frame per column tick, every symbol in full.** The push loop drains `state` every
  `1/hz` s (default 4 Hz, matching the matplotlib column cadence) and broadcasts a JSON frame
  with an `order` list and a `symbols` map — each entry carrying that symbol's `bids`/`asks`
  arrays, `mid`/`spread`/`imbalance`, the *new* `trades` since last tick, running `cvd`, and
  per-column `delta`. Sending all watched symbols (not just a focus) is what lets the browser
  render a grid. CVD/delta are accumulated server-side *per symbol* so a late-joining browser
  is consistent; draining always (even with no clients) keeps CVD continuous and the per-
  symbol trade deques self-bound.
- **The browser owns the matrices.** `web/app.js` keeps one rolling price×time matrix *per
  symbol* (a `View`) client-side and paints each the way GPU heatmaps do — a 1px-per-cell
  offscreen image scaled up with smoothing off — overlaying mid/bid/ask, spread band, trade
  bubbles, a volume profile, a Δ strip and a CVD strip as vectors. Each `View` auto-fits its
  price band to ≈0.15% of price and supports wheel / `+`/`−` zoom (a zoom re-bins, like the
  matplotlib `+`/`−`); recenter is a vertical roll of the stored columns, so history stays
  price-anchored.
- **Layout: grid + expand (5A Tier 2).** One symbol → the full view. Several → a grid of
  live mini-heatmaps (all on screen at once); click a tile to expand to the full view, Esc to
  return. Because every symbol streams continuously, expanding is purely client-side — no
  server round-trip.
- **Control.** With a multi-symbol `--watchlist` the bridge subscribes to all of them up
  front (`run_schwab_stream(symbols=…)` → `set_subscribed`); `--simulate --watchlist …` runs
  an independent synthetic walk per symbol for offline grid testing. A
  `{"type":"switch","symbol":…}` adds a symbol not yet watched (live) or, in single-symbol
  mode, resubscribes exactly as the matplotlib UI does. No-op in sim/replay (no `control`).
- **Deps & fallback.** Needs `websockets` (optional; matplotlib renderer doesn't). The
  frontend is dependency-free vanilla canvas served by a tiny stdlib HTTP server, so there
  is no build step and nothing fetched from a CDN. matplotlib stays the default.

## Roadmap

| Phase | Scope | State |
|-------|-------|-------|
| 0 | Docs & hygiene | done |
| 1 | Extract reusable book layer + tests | done |
| 2 | Price-window recentering / auto-zoom | done |
| 3 | Trades layer (T&S bubbles + CVD) | done |
| 4 | Recording & replay | done |
| 5A.1 | Hot ticker switching (live, no restart) | done |
| 5C | Wall / iceberg / pull detection | done |
| 5D | Analytics panels (volume profile/VPOC, Δ footprint, bid/ask+spread band, legend, absorption flag) | done |
| 5B | Web/canvas renderer (`bridge.py` + `web/`, vanilla-canvas slice) | done |
| 5A.2 | Simultaneous multi-symbol (browser focus+sidebar) | done |

## Phase 5 proposals (need sign-off before building)

The data layer is already renderer-agnostic, so these are additive — none require touching
`orderbook.py` / `state.py` / `feeds.py`. Three tracks: **5A** ticker switching &
multi-symbol, **5B** web/canvas renderer, **5C** wall/iceberg detection. Full design,
sizing, and build order are in **[PHASE5.md](PHASE5.md)**.

All three tracks are now implemented: **5A** (Tier 1 hot switching in matplotlib; Tier 2
simultaneous multi-symbol in the browser), **5B** (web renderer — `bridge.py` + `web/`), and
**5C** (wall/iceberg detection). See the matching sections above. PHASE5.md lists the
remaining optional polish (Lightweight-Charts frontend, msgpack payloads, runtime add/remove
of watched symbols).
