# Galyari — Live Equity Order-Flow Heatmap

A free, Linux-native, real-time **order-flow / liquidity heatmap** for US equities — a
Bookmap-style view of the limit order book over time. Price runs up the y-axis, time
scrolls right-to-left across the x-axis, and color intensity is the resting size at each
price level. The goal is to *see* market microstructure that a candlestick chart hides:
where liquidity rests, where walls build and get pulled, absorption, and imbalance.

![mode: simulate](https://img.shields.io/badge/offline_mode-simulate-blue) ![data: Schwab NASDAQ_BOOK](https://img.shields.io/badge/live_data-Schwab%20NASDAQ__BOOK-orange)

## Why this exists

Commercial order-flow tools (Bookmap, ATAS, Sierra) are Windows-centric and gate live
equity depth behind paid data subscriptions. If you already have a Charles Schwab
brokerage account with API access, Schwab's streaming API exposes Level 2 book data
(`NASDAQ_BOOK`) over WebSocket. This project renders the same class of visualization from
data you already have, on Linux, for free.

There are two intended audiences, and the architecture serves both:

1. **Discretionary use** — a live window to watch liquidity while trading or observing.
2. **Systematic research** — the *parsed* order book ([`orderbook.py`](orderbook.py)) is a
   clean, importable, matplotlib-free module. Other research projects can consume parsed
   book frames to build features (book imbalance, wall persistence, absorption events)
   without dragging in the renderer.

The parsing / book-state layer is the **stable core**; the renderer is **replaceable**.

## Install

Requires Python 3.10+ (developed on 3.13).

```bash
pip install -r requirements.txt
```

## Configure (live mode only)

Simulate mode needs **no credentials**. For live Schwab data, create a `.env` in this
folder (a template ships as `.env`; never commit the real one — it's gitignored):

```
SCHWAB_API_KEY=...            # App Key from developer.schwab.com
SCHWAB_APP_SECRET=...         # App Secret
SCHWAB_CALLBACK_URL=https://127.0.0.1:8182/   # MUST match the app registration char-for-char, incl. trailing slash
SCHWAB_ACCOUNT_ID=...         # your brokerage account id (digits)
SCHWAB_TOKEN_PATH=/abs/path/to/token.json     # absolute path; reused across runs
SCHWAB_SYMBOL=GOOG            # optional default symbol
```

> **The trailing slash on the callback URL matters.** A missing `/` produces "We are
> unable to complete your request" *after* you log in. The registered URL and the `.env`
> value must match character-for-character.

## Run

**Offline / no credentials (start here):**

```bash
python schwab_orderflow_heatmap.py --simulate
```

This drives a synthetic random-walk book with decaying liquidity walls. It needs no
Schwab account and no market hours, so it is the always-available test harness.

**Live:**

```bash
python schwab_orderflow_heatmap.py                 # uses SCHWAB_SYMBOL from .env
python schwab_orderflow_heatmap.py --symbol AAPL
python schwab_orderflow_heatmap.py --raw           # dump one book frame, then run normally
```

On first live run an OAuth flow prints an auth URL. Open it, log in, and paste the full
redirect URL (`https://127.0.0.1:8182/?code=...`) back into the terminal. The browser
page will look broken/unreachable — that's expected, nothing is listening on the
callback; the authorization code is in the URL. A `token.json` is written and reused
afterward.

> **Reauth cadence:** the access token (30 min) refreshes automatically and silently;
> the **refresh token lasts ~7 days** and is not rolled forward, so you only redo the
> paste-the-URL flow about **once a week**. In between, start/stop freely with no auth
> interaction as long as `token.json` is under 7 days old.

### Switch symbols live (no restart)

In live mode you can retarget the view on the fly:

```bash
python schwab_orderflow_heatmap.py --symbol AAPL --watchlist AAPL,GOOG,TSLA,NVDA
```

- **`n` / `p`** — cycle forward/back through the watchlist.
- **`go to ▸` box** (top-right) — type any symbol + `Enter` to jump there (it's added to
  the watchlist).

The heatmap clears and re-auto-fits on the new symbol; a guard drops any late frames from
the previous symbol so nothing stale flashes. (Single symbol per process still — this
switches *which* one; simultaneous multi-symbol is Phase 5 Tier 2.) `--watchlist` can also
come from `SCHWAB_WATCHLIST` in `.env`.

### Record & replay

```bash
python schwab_orderflow_heatmap.py --simulate --record      # writes recordings/SIM_*.jsonl
python schwab_orderflow_heatmap.py --symbol AAPL --record    # records a live session
python schwab_orderflow_heatmap.py --replay recordings/AAPL_1782144614895.jsonl
python schwab_orderflow_heatmap.py --replay <file> --speed 4 # 4× faster playback
```

The recording is an append-only JSONL event log (one frame per line, never rewritten);
replay feeds it back through the **same** shared-state plumbing as live/sim, so the
renderer is identical across all three modes.

### Browser renderer (Phase 5B)

An alternative front-end: instead of the matplotlib window, `bridge.py` launches the same
producer (sim / live / replay) and streams each frame over a WebSocket to a browser canvas.
The Python data layer is unchanged — this adds a publisher + a static frontend only, and is
the home for smoother scrolling and (later) simultaneous multi-symbol layouts.

```bash
pip install websockets                              # one-time, web renderer only
python bridge.py --simulate                         # then open the printed URL
python bridge.py --symbol AAPL --watchlist AAPL,GOOG,TSLA
python bridge.py --replay recordings/AAPL_*.jsonl --speed 4
```

Open **http://127.0.0.1:8080** in any browser. The page is a pure view (vanilla canvas, no
build step, no external libraries), styled like a Bookmap window — **blue heatmap on navy**,
green/red bubbles and bid/ask lines. A single symbol shows the **full view**: heatmap +
volume profile/POC + mid line + bid/ask touch + spread band + trade bubbles + a right-edge
**price ladder** (per-level resting size as green/red bars) + Δ footprint + CVD strip, with
price labels on both edges. **Scroll the wheel over the chart (or press `+`/`−`) to zoom the
price axis** — liquidity is stored at absolute price bins, so zooming is a lossless crop
(zoom out and prior history is already there, never rebuilt). In live mode, type a symbol in
`go to ▸` to switch. Ports are configurable (`--web-port`, `--ws-port`); data rate is `--hz`
(default 4). The matplotlib renderer remains the default.

**Simultaneous multi-symbol grid (Phase 5A Tier 2, browser only).** Pass a `--watchlist`
with more than one symbol and the bridge streams all of them at once; the browser shows a
**grid of live heatmaps — all symbols on screen together**:

```bash
python bridge.py --symbol AAPL --watchlist AAPL,GOOG,TSLA,NVDA   # 2×2 grid, live
python bridge.py --simulate  --watchlist ACME,BETA,GAMMA,DELTA   # same grid, OFFLINE
```

**Click any tile to expand** it to the full view; **Esc** (or the `‹ grid` button) returns
to the grid. Scroll zooms the tile under the cursor. Typing a symbol not yet watched adds it
live. The offline form (`--simulate --watchlist …`) drives an independent synthetic walk per
symbol, so you can exercise the whole grid with no Schwab account and no market hours. The
matplotlib renderer stays single-symbol. Practical cap is **8 symbols** (`MAX_SYMBOLS`) —
book data is heavy per symbol and Schwab caps the overall streaming rate, so a handful is the
sweet spot.

### Useful flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--simulate` | off | synthetic book + trades, no credentials |
| `--replay FILE` | — | play back a recorded JSONL tape |
| `--speed X` | `1.0` | replay speed multiplier |
| `--record` | off | append frames to `recordings/` |
| `--symbol SYM` | `$SCHWAB_SYMBOL` or `GOOG` | symbol to subscribe |
| `--watchlist A,B,C` | `$SCHWAB_WATCHLIST` | symbols to cycle with `n`/`p` (live) |
| `--tick T` | `0.01` | price bin size (dollars) |
| `--rows N` | auto | price bins shown (default: an auto price band ≈0.15% of price; override to pin a height) |
| `--cols N` | `240` | number of time columns (history width) |
| `--no-trades` | off | hide the trades layer (book only) |
| `--no-micro` | off | disable wall/iceberg/pull detection overlays |
| `--raw` | off | dump one raw book frame then continue |
| `--diag` | off | print live book + level-one + heatmap stats (for diagnosing) |

### Keyboard controls (in the window)

| Key | Action |
|-----|--------|
| `+` / `−` | zoom the price band in / out (a display crop — history is preserved, so zooming out reveals already-recorded liquidity) |
| `m` | toggle the wall/iceberg/pull overlays |
| `n` / `p` | cycle the watchlist (live mode) |

### What you see

The window is a 3×3 grid: the heatmap fills the main panel, with a **volume profile** in
the right margin, a **delta footprint** strip below it, and the **CVD** panel at the
bottom. A color/marker **legend** sits in the empty lower-right.

- **Heatmap** — resting size at each price (brighter = more liquidity); walls show as
  bright horizontal streaks, pulls as streaks that suddenly go dark. Color uses a
  power-law scale (small sizes stay visible on thin books). The visible price band is a
  fixed window centered on the touch — `+`/`−` zoom it; equity books are sparse, so expect
  ~10–15 streaks near the price, not a dense wall.
- **Mid line** — mid price over time (light, with a soft halo so it reads over hot cells).
  The y-axis auto-recenters as price drifts. Price labels run down **both** edges.
- **Bid/ask touch + spread band** — faint green (best bid) and red (best ask) staircases
  with a translucent band filling the spread between them, so support-below vs
  resistance-above and a *widening* spread read at a glance.
- **Bubbles** — trade prints; area scales with size *relative to the recent typical print*
  (so small-lot names still show variation). Green = buyer-initiated (lifted the offer),
  red = seller-initiated (hit the bid), gray = ambiguous. See the caveat below.
- **Volume profile (right margin)** — total *traded* volume by price over the on-screen
  window, sharing the heatmap's price axis. The yellow tick marks the **point-of-control**
  (most-traded price). Where the heatmap shows resting *intent*, this shows where business
  actually happened — high-volume nodes are strong support/resistance.
- **Delta footprint (Δ strip)** — per-time-bucket net buy−sell volume as green/red bars, so
  aggression bursts and exhaustion pop out instantly. (The CVD line below answers a
  different question — the *running total* — so both are shown.)
- **CVD panel** — cumulative volume delta (running Σ of signed trade size).
- **Wall / iceberg / pull overlays** (Phase 5C) — `◄` marks a persistent **wall** (large,
  long-resting level; green=bid, red=ask, brighter=more persistent), `◆` a likely
  **iceberg** (a level that keeps refilling under heavy execution), and an amber `✕` flags
  a **pull** (a wall that just vanished). Toggle with `m`; disable with `--no-micro`.
- **Header** — symbol, mid, spread, book imbalance, CVD, live wall/iceberg/pull counts, and
  a conservative **`⚠ ABSORPTION ↑/↓`** flag (text-only, no marker spam): it fires when a
  big one-sided delta burst (≥ 8× the typical print) is met by ≤ 2 ticks of price movement
  — aggression hitting a wall that holds. `↑` = sellers absorbed (buyers defending);
  `↓` = buyers absorbed.

> **Live trades are derived from `LEVEL_ONE_EQUITY`**, not a true tick-by-tick tape —
> Schwab's streamer has no time-of-sale service. This is fine for visualization but is an
> approximation for trade-level research. See
> [DATA_SCHEMA.md](DATA_SCHEMA.md#trades-derived-from-level_one_equity).

## Market-hours & entitlement caveats

Live book data flows **only during the regular session (~9:30–16:00 ET)** and requires
your account to carry a **Level 2 / book entitlement**. **Silence after the `subscribed`
log line is normal** when the market is closed or you lack the entitlement — it is not a
bug, and an empty book is never treated as an error. Use `--simulate` any time.

> Harmless noise: `MESA-INTEL ... FINISHME` lines are Intel GPU/Vulkan warnings from the
> matplotlib window. Ignore them.

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md) — threading model, shared-state contract, data flow,
  the renderer/data seam, and known limitations.
- [DATA_SCHEMA.md](DATA_SCHEMA.md) — the `NASDAQ_BOOK` wire schema, the snapshot-vs-delta
  finding, and the parsed `Book` structure.

## Status

See [ARCHITECTURE.md](ARCHITECTURE.md#roadmap) for the phase roadmap. All phases are
implemented: parsing/book-state core, recentering auto-zoom heatmap, trades layer
(bubbles + CVD), record/replay, live hot ticker-switching, wall/iceberg/pull detection,
analytics panels (volume profile/VPOC, Δ footprint, bid/ask + spread band, legend,
absorption flag), a WebSocket **browser renderer** ([`bridge.py`](bridge.py) + [`web/`](web)),
and **simultaneous multi-symbol** (browser grid). Design notes for the later phases are in
[PHASE5.md](PHASE5.md).

## Disclaimer

Galyari is a visualization and research tool, not trading or investment advice. It ships
with no warranty; use at your own risk. It is not affiliated with or endorsed by Charles
Schwab — "Schwab" and "NASDAQ_BOOK" are referenced only to describe the data source. You
are responsible for complying with your brokerage's API terms and market-data agreements.

## License

[MIT](LICENSE).
