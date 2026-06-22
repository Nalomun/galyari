# Phase 5 — Detailed Proposals (need sign-off before building)

Phase 5 is optional/future work. Nothing here requires touching the stable core
(`orderbook.py`, `state.py`, `feeds.py`); these are additive. Three tracks, independent
but with natural pairings noted. Each is sized and broken into the smallest shippable
slice first.

Tracks:
- **5A — Ticker switching & multi-symbol** (the near-term ask)
- **5B — Web/canvas renderer**
- **5C — Liquidity-wall / iceberg detection**

---

## 5A — Ticker switching & multi-symbol

Today: one symbol per process; changing symbol means restarting with `--symbol`. This is
the highest-value Phase 5 item for day-to-day use and splits into two tiers. **Do Tier 1
first** — it's small and delivers the "switch easily" need on its own.

### Tier 1 — Hot ticker switching (single view) — ✅ IMPLEMENTED

Keep the single heatmap; let the subscribed symbol change at runtime, no restart.

> **Status: built.** `n`/`p` cycle a `--watchlist`; the `go to ▸` box jumps to any symbol.
> `feeds.StreamControl` hops commands onto the stream loop (`call_soon_threadsafe`); the
> loop resubscribes inline with a 0.5 s poll so quiet markets stay responsive; a
> `state` active-symbol guard drops stale frames from the old symbol. Tested in
> `tests/test_switching.py`. The design notes below are kept for reference.

**What changes**

- `state.py`: add `clear_for_symbol(sym)` — atomically drop the book + trades and record
  the active symbol, so a stale book from the old name never flashes on screen.
- `feeds.py`: the Schwab stream loop runs asyncio on a background thread. Add a
  thread-safe control channel (an `asyncio.Queue` populated via
  `loop.call_soon_threadsafe`) that the renderer pushes `("switch", new_sym)` onto. On
  receipt the loop does:
  ```python
  await stream.nasdaq_book_unsubs([old]);  await stream.nasdaq_book_subs([new])
  await stream.level_one_equity_unsubs([old]); await stream.level_one_equity_subs([new])
  ```
  (schwab-py exposes the matching `*_unsubs` calls.) The simulator/replay ignore switches
  or, for sim, just reseed.
- Renderer: a matplotlib key handler — `n`/`p` to cycle a `--watchlist AAPL,GOOG,TSLA`,
  and a `matplotlib.widgets.TextBox` to type an arbitrary symbol. On switch: clear the
  `Heatmap` (matrix, `mid_hist`, `cvd`, trades, `row0_price`), update the window title and
  header, send the control message. The heatmap re-autofits on the first new book.

**Cost / risk:** low. The only fiddly bit is crossing the thread→asyncio boundary safely;
`call_soon_threadsafe` is the standard answer. Replay/sim need a no-op switch path.

**Result:** press `n`/`p` or type a ticker to retarget the live view in ~1 second.

### Tier 2 — Simultaneous multi-symbol (bigger; pairs with 5B)

Track several books at once.

**What changes**

- `state.py`: generalize `_book` → `dict[symbol -> Book]` and trades → per-symbol ring
  buffers. `get_book(symbol)`, `drain_trades(symbol)`. (`feeds._last_trade` is *already*
  keyed by symbol, and book/level-one messages already carry `key` per entry — routing is
  basically free.)
- `feeds.py`: subscribe lists — `nasdaq_book_subs(syms)`, `level_one_equity_subs(syms)`.
  Handlers route each content entry by its `key`.
- Renderer layout, two options:
  - **(a) Small-multiples grid** — an N×M grid of mini-heatmaps. Best for monitoring a
    watchlist at a glance; each tile loses detail.
  - **(b) Focus + sidebar** *(recommended)* — one full heatmap plus a column of compact
    tiles (sparkline of mid + imbalance/CVD chips) per watched symbol; click a tile to
    promote it to the main view. Monitor many, focus one.

**Limits to respect:** Schwab caps symbols-per-subscription and overall streaming data
rate, and book data is heavy per symbol — practically a handful of symbols, not dozens.
Document the cap once measured. matplotlib handles ~4–6 mini-heatmaps at 3 Hz; beyond
that, render in the browser (5B). This is why Tier 2 and 5B are best built together.

---

## 5B — Web/canvas renderer

matplotlib `FuncAnimation` + `imshow` + `np.roll` at 3 Hz is fine as a seed but is the
ceiling: no smooth pan/zoom, limited FPS, redraw cost grows with panel count. A browser
canvas is the right long-term substrate. **The Python data layer is unchanged** — this
adds a publisher and a frontend only.

**Shape**

```
state.py  ──▶  bridge.py (asyncio WebSocket server)  ──▶  browser (canvas)
              serializes latest Book + trades + CVD       renders; sends control msgs
              at ~10–20 Hz                                 (switch symbol, pan, zoom)
```

- `bridge.py`: an `asyncio` WebSocket server (e.g. `websockets`/`aiohttp`) that, each
  tick, reads the same `state.py` getters and pushes a **frame message**:
  ```json
  {"symbol":"GOOG","ts":1782144614895,
   "bids":[[344.80,120],[344.67,200]], "asks":[[344.90,100]],
   "mid":344.85, "trades":[{"p":344.90,"s":300,"side":1}], "cvd":-300}
  ```
  Start with JSON; if bandwidth bites, switch the array payloads to msgpack / binary typed
  arrays (the schema stays the same). The browser is a pure view; Python stays
  authoritative. The control channel from 5A is reused for symbol switching from the UI.
- Frontend, two paths:
  - **Lightweight Charts v5 + a custom series primitive** *(recommended start)* — you get
    the time axis, crosshair, price scale, panes, and trade markers for free; the heatmap
    is a custom primitive that paints colored cells on the price/time grid, and CVD is a
    second pane. Trades render as series markers or their own primitive.
  - **Raw WebGL (regl / PixiJS)** — upload the rolling matrix as a texture and shift the
    last column each frame (how GPU heatmaps work). Maximum throughput, more code, no
    charting niceties out of the box. Reach for this only if LWC's primitive can't keep up.

**Cost / risk:** medium–high; the new surface is the JS app. Keep the matplotlib renderer
as the default/fallback so the project never depends on a running browser.

---

## 5C — Liquidity-wall / iceberg detection — ✅ IMPLEMENTED

> **Status: built** in `microstructure.py` (stdlib-only consumer of the core), with
> renderer overlays (`◄` wall, `◆` iceberg, scrolling `✕` pull), a header count, an `m`
> toggle, `--no-micro`, per-symbol reset on hot switch, and pull events written to the
> event log under `--record`. Tuned for precision (refills must follow execution).
> Tested in `tests/test_microstructure.py`. Design notes kept below for reference.

A feature module (`microstructure.py`) that **consumes** the parsed `Book`/`Trade` stream
— pure use of the existing seam, no core changes — and emits events + renderer overlays.
This is also the first real consumer of the per-venue breakdown already preserved on
`Level`.

**Signals**

- **Wall:** a price level whose resting size stays large and *persistent* across a sliding
  window. Persistence score = fraction of the last N frames in which the level is present
  with size > threshold. Surface the top-scoring levels as horizontal overlays.
- **Pull:** a tracked wall whose size collapses suddenly (size drop > X% in one/few
  frames) — annotate the moment; often precedes a fast move.
- **Iceberg:** a level that keeps **executing** (trades printing at that price) yet
  **refills** its displayed size — i.e. cumulative traded volume at a price ≫ its
  displayed size over a window, with repeated replenishment. Requires joining trades to
  the book; the per-venue `SEQUENCE`/refill pattern can sharpen *which* venue is
  reloading.

**Output**

- Append detected events to the JSONL event log (same tape as Phase 4) for offline study.
- Renderer overlays: a marker/halo at wall prices that fades on pull; an icon at suspected
  icebergs.

**Testability (offline):** extend the simulator to plant deterministic walls (it already
decays walls) and a scripted iceberg (a level that absorbs repeated prints and refills),
then assert the detector fires within K frames. No live session needed.

**Cost / risk:** low–medium and self-contained; mostly tuning thresholds. Good standalone
research deliverable independent of 5A/5B.

---

## Suggested order

1. ~~**5A Tier 1 (hot switching)**~~ — ✅ done.
2. ~~**5C (wall/iceberg)**~~ — ✅ done.
3. **5B + 5A Tier 2 together** — the web renderer is the right home for simultaneous
   multi-symbol layouts and the FPS they need.
