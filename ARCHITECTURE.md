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
                              │  imshow + mid line + trade bubbles+CVD  │
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

A tiny module holding the latest `Book` and a bounded trade buffer behind one
`threading.Lock`.

- **Writers** (producers): `set_book(book)`, `add_trade(trade)`.
- **Reader** (render loop): `get_book()`, `drain_trades()`.
- The store keeps only the **latest** book (full snapshot — see DATA_SCHEMA) and a small
  ring of recent trades. There is no history kept here; history lives in the renderer's
  rolling matrix and (optionally) the recording file.
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
   colored by aggressor side; CVD subpanel integrates signed volume.
4. `MicrostructureAnalyzer.update(book, trades)` → walls / pulls / icebergs, drawn as
   right-edge markers + scrolling pull flags (Phase 5C; see below).
5. `imshow.set_data`, autoscale color limits, redraw mid line + y tick labels.

## Modules

| File | Role | Imports matplotlib? |
|------|------|---------------------|
| `orderbook.py` | **Stable core.** Typed `Book`/`Level`/`Trade`, `parse_nasdaq_book(msg)`, `OrderBook` state. Dependency-light; the seam research code imports. | No |
| `state.py` | Thread-safe shared store between producer and renderer. | No |
| `feeds.py` | Producers: live Schwab stream, simulator, replay reader; `StreamControl` for hot switching. | No |
| `recorder.py` | Append-only JSONL recording of raw frames. | No |
| `microstructure.py` | **Consumer of the core.** Wall / pull / iceberg detection over the `Book`/`Trade` stream. Stdlib only. | No |
| `schwab_orderflow_heatmap.py` | CLI entrypoint + matplotlib renderer (`Heatmap`). | Yes |

The rule: **nothing under "stable core / producers" imports matplotlib.** A different
frontend (web/canvas) could replace only `schwab_orderflow_heatmap.py`.

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

## Known limitations

- **matplotlib** is fine as a seed renderer but not ideal for a high-FPS scrolling
  heatmap; a web/canvas frontend is the eventual path (Phase 5, propose-only).
- **Single symbol** per process.
- Color autoscaling is per-frame on `matrix.max()`; a persistent percentile scale would be
  steadier but this is good enough and adapts to regime changes.
- The per-venue breakdown of each level is parsed and available on `Level` but not yet
  visualized.

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
| 5 (rest) | Simultaneous multi-symbol / web renderer | proposed — see below |

## Phase 5 proposals (need sign-off before building)

The data layer is already renderer-agnostic, so these are additive — none require touching
`orderbook.py` / `state.py` / `feeds.py`. Three tracks: **5A** ticker switching &
multi-symbol, **5B** web/canvas renderer, **5C** wall/iceberg detection. Full design,
sizing, and build order are in **[PHASE5.md](PHASE5.md)**.

**5A Tier 1 (hot ticker switching)** and **5C (wall/iceberg detection)** are implemented —
see "Hot ticker switching" and "Microstructure detection" above. The remaining tracks (5A
Tier 2 simultaneous multi-symbol, 5B web renderer) are still proposals.
