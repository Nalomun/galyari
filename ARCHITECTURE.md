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
4. `imshow.set_data`, autoscale color limits, redraw mid line + y tick labels.

## Modules

| File | Role | Imports matplotlib? |
|------|------|---------------------|
| `orderbook.py` | **Stable core.** Typed `Book`/`Level`/`Trade`, `parse_nasdaq_book(msg)`, `OrderBook` state. Dependency-light; the seam research code imports. | No |
| `state.py` | Thread-safe shared store between producer and renderer. | No |
| `feeds.py` | Producers: live Schwab stream, simulator, replay reader. | No |
| `recorder.py` | Append-only JSONL recording of raw frames. | No |
| `schwab_orderflow_heatmap.py` | CLI entrypoint + matplotlib renderer (`Heatmap`). | Yes |

The rule: **nothing under "stable core / producers" imports matplotlib.** A different
frontend (web/canvas) could replace only `schwab_orderflow_heatmap.py`.

## Auth (left as-is, locked)

Manual OAuth (`client_from_manual_flow`) runs on the **main thread before the window
opens**, so the redirect-URL paste happens against a clean terminal. Credentials come from
`.env` via `python-dotenv`. `token.json` (with refresh token) is written once and reused;
schwab-py refreshes it. Both files are gitignored. No migration, no rewrite — this works.

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
| 5 | Web renderer / multi-symbol / wall detection | proposed only — see below |

## Phase 5 proposals (need sign-off before building)

The data layer is already renderer-agnostic, so these are additive — none require
touching `orderbook.py` / `state.py` / `feeds.py`.

- **Web/canvas renderer.** Stand up a small WebSocket bridge that publishes the same
  snapshots `state.py` holds; render in the browser with Lightweight Charts v5 + a custom
  heatmap primitive. Far better FPS and pan/zoom than matplotlib for a scrolling heatmap.
  Cost: a new frontend; the Python side only gains a `bridge.py` publisher.
- **Multi-symbol.** Generalize the shared store from a single `Book` to a `dict[symbol]`
  and run one producer subscription list; renderer gets a symbol selector / small-multiples.
  Touches `state.py` and the renderer; parser unchanged.
- **Liquidity-wall / iceberg detection.** A feature module over the parsed `Book` stream:
  track per-price persistence and refill-after-trade to flag walls and likely icebergs,
  surfaced as overlays. Pure consumer of the existing seam; good first research use of the
  per-venue breakdown already preserved on `Level`.
