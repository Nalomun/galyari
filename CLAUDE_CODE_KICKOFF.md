# Claude Code Kickoff — Live Equity Order-Flow Heatmap

You are picking up a working seed and growing it into a maintainable project. Read this whole brief first, then read the seed file `schwab_orderflow_heatmap.py` in this directory before writing anything. Your first deliverable is documentation (Phase 0); do not jump ahead to features.

---

## 1. What this is and why it exists

This is a free, Linux-native, real-time **order-flow / liquidity heatmap for US equities** — a Bookmap-style view of the limit order book over time. The y-axis is price, the x-axis is time scrolling left, and color intensity is resting size at each price level. The point is to *see* market microstructure that a candlestick chart hides: where liquidity is resting, where walls build and get pulled, absorption, and imbalance.

The motivation: commercial order-flow tools (Bookmap, ATAS, Sierra) are Windows-centric and gate live equity depth behind paid data subscriptions. The owner already has a Charles Schwab brokerage account with API access, and Schwab's streaming API exposes Level 2 book data (`NASDAQ_BOOK`) over WebSocket. So instead of paying for Bookmap + a Nasdaq TotalView feed, this renders the same class of visualization from data the owner already has, on Linux, for free.

There are two audiences for the output, and the architecture should serve both:
1. **Discretionary use** — a live window to watch liquidity while trading/observing.
2. **Systematic research** — the *parsed* order book is a reusable data structure. Other research projects (a systematic intraday bot, and a newer microstructure-research effort) may want to consume parsed book frames to build features (book imbalance, wall persistence, absorption events). So the parsing/book-state layer must be a clean, importable module, not tangled into the renderer.

This is a standalone project. Do **not** couple it to the owner's other repos. Auth is self-contained via `.env` and works; leave it as-is.

---

## 2. Current state — the seed (works end to end)

`schwab_orderflow_heatmap.py` is a single file (~270 lines) that was debugged to a working state in a live session. It currently does:

- **Data ingestion (live):** a background thread runs `schwab-py`'s asyncio stream client, subscribes to `NASDAQ_BOOK` for one symbol, and on each message parses bids/asks into `list[(price, volume)]`, writing the latest book into a shared dict guarded by a `threading.Lock`.
- **Data ingestion (simulate):** a `--simulate` mode generates a synthetic random-walk book with decaying liquidity "walls," writing to the same shared state. This needs no credentials and no market hours, so **it is the always-available test harness** — keep it working through every change.
- **Rendering:** the matplotlib main thread runs a `FuncAnimation` at ~3 Hz (300 ms). Each tick it snapshots the shared book into one column of a rolling `(n_price_bins, n_time_cols)` numpy matrix (via `np.roll`), and draws it with `imshow` (inferno colormap) plus a cyan mid-price line.
- **Auth:** manual OAuth flow (`client_from_manual_flow`) on the main thread before the window opens — prints an auth URL, the user pastes back the redirect URL, a `token.json` is written and reused thereafter. Credentials come from `.env` via `python-dotenv`.

### Architecture in one sentence
Background producer thread (Schwab or simulator) → shared locked `Book` state → matplotlib consumer loop bins into a rolling matrix and renders.

### Known limitations (intentionally deferred to you)
- The price window is anchored on the first observed mid and **does not recenter** — if price drifts out of the window it clips. This is the single most important thing to fix (Phase 2).
- Single symbol only.
- No trades layer (no time-and-sales, no CVD, no trade bubbles) — the heatmap shows resting limit liquidity only.
- No recording/replay.
- matplotlib is fine for a seed but is not a great long-term renderer for a high-FPS scrolling heatmap.

---

## 3. Confirmed technical facts (locked — do not re-derive)

These were verified against a live Schwab feed this session. Treat them as ground truth.

### `NASDAQ_BOOK` message schema (as relabeled by schwab-py)
Each message has `content`, a list with one entry per subscribed symbol. The entry looks like this (real captured GOOG frame, trimmed — use it as a test fixture):

```json
{
  "key": "GOOG",
  "BOOK_TIME": 1782144614895,
  "BIDS": [
    {
      "BID_PRICE": 344.80,
      "TOTAL_VOLUME": 120,
      "NUM_BIDS": 1,
      "BIDS": [
        { "EXCHANGE": "NSDQ", "BID_VOLUME": 120, "SEQUENCE": 43814837 }
      ]
    },
    {
      "BID_PRICE": 344.67,
      "TOTAL_VOLUME": 200,
      "NUM_BIDS": 2,
      "BIDS": [
        { "EXCHANGE": "edgx", "BID_VOLUME": 120, "SEQUENCE": 43814848 },
        { "EXCHANGE": "batx", "BID_VOLUME": 80,  "SEQUENCE": 43814848 }
      ]
    }
  ],
  "ASKS": [
    {
      "ASK_PRICE": 344.90,
      "TOTAL_VOLUME": 100,
      "NUM_ASKS": 1,
      "ASKS": [
        { "EXCHANGE": "arcx", "ASK_VOLUME": 100, "SEQUENCE": 43814900 }
      ]
    }
  ]
}
```

Key points:
- Top level per side: `BIDS` / `ASKS` arrays of **price levels**, sorted best-to-worse.
- Each price level: `BID_PRICE`/`ASK_PRICE`, `TOTAL_VOLUME` (aggregated across venues at that price), `NUM_BIDS`/`NUM_ASKS`, and a **nested same-named array** breaking the level down per venue (`EXCHANGE`, `BID_VOLUME`/`ASK_VOLUME`, `SEQUENCE`).
- `BOOK_TIME` is epoch milliseconds.
- Observed venues: `NSDQ, arcx, edgx, batx, nyse, memx, miax` — so this is a multi-venue aggregated book surfaced through Schwab, not Nasdaq-only. The per-venue breakdown is currently discarded by the seed but is worth preserving in the parser (could matter for research).
- The seed treats each message's `BIDS`/`ASKS` as the **complete current book** (full replace). Verify whether Schwab ever sends partial/delta updates; if it's always full snapshots, the full-replace model is correct and simpler. Document your finding.

### Operational facts
- **Callback URL must match the Schwab app registration character-for-character, including the trailing slash.** A missing `/` produced "We are unable to complete your request" after login. The `.env` ships the slash; preserve it.
- Live data only flows during the **regular session (~9:30–16:00 ET)** and requires the account to have a **Level 2 / book entitlement**. Silence after the "subscribed" log line means market closed or no entitlement — not a bug. Never treat empty book as an error state.
- Field-name parsing was confirmed correct against the live frame above; the parser does not need fixing.
- `--raw` dumps one frame then goes quiet (one-shot); it was previously flooding the terminal.

---

## 4. Locked decisions (do not relitigate)

- **Keep the current `.env` + manual-flow auth.** It works. No migration to other repos, no rewrite of auth.
- **Standalone project**, Linux, Python, `schwab-py`.
- **Simulate mode is the test harness** and must remain runnable with zero credentials at all times.
- The **data/parser layer is the stable core**; the **renderer is replaceable**. Design the seam so the renderer could be swapped (e.g. to a web/canvas frontend) without touching ingestion or book state.

---

## 5. Build phases (do these in order; confirm phase boundaries before moving on)

### Phase 0 — Documentation & repo hygiene (do this first)
- Read the seed thoroughly.
- Produce your own docs from scratch, written so a future reader understands both the technicals and the *why* in section 1:
  - `README.md` — what it is, the goal, install, `.env` setup, how to run simulate vs live, the market-hours/entitlement caveats.
  - `ARCHITECTURE.md` — the threading model, the shared-state contract, the data flow, the renderer/data seam, and the known limitations.
  - `DATA_SCHEMA.md` — the `NASDAQ_BOOK` schema above, snapshot-vs-delta finding, and the parsed `Book` structure you settle on.
- Add `requirements.txt` (numpy, matplotlib, schwab-py, python-dotenv) and a `.gitignore` that excludes `token.json`, `.env`, and `__pycache__/`.
- Do not change behavior in this phase.

### Phase 1 — Extract the reusable book layer
- Pull parsing + book state into its own module (e.g. `orderbook.py`): a typed `Book`/level structure, a `parse_nasdaq_book(msg) -> Book` function, and an `OrderBook` state class that holds current bids/asks (and optionally the per-venue breakdown).
- Add unit tests using the captured GOOG frame as a fixture (`pytest`). This module is the seam other research projects will import, so it must be clean and dependency-light (no matplotlib import).
- The main script imports from it; behavior unchanged.

### Phase 2 — Fix price-window recentering/auto-zoom (highest-value feature)
- Replace the fixed-anchor window with absolute-price-keyed storage so the y-axis can follow price drift without losing history. When mid moves, shift/recenter the matrix rows rather than clipping.
- Add sensible auto-zoom on the price range (e.g. track recent mid ± a configurable band, or fit to observed depth), with a manual override.
- Verify against `--simulate` (its random walk will drift out of the old window quickly — that's the test).

### Phase 3 — Trades layer (time-and-sales)
- Subscribe to the relevant Schwab time-of-sale / trade stream alongside the book.
- Overlay **trade bubbles** (size ∝ trade size, color by aggressor side — at/above ask = buy, at/below bid = sell).
- Add a **CVD (cumulative volume delta)** subpanel.
- Extend simulate mode to emit synthetic trades so this is testable offline.

### Phase 4 — Recording & replay
- Record raw incoming frames to an **append-only, immutable event log** (JSONL or parquet, timestamped) — this matches the owner's preferred event-log pattern and enables faithful replay.
- Add a `--replay <file>` mode that feeds recorded frames into the same shared-state plumbing the simulator uses, so the renderer is identical for live/sim/replay.

### Phase 5 — Optional / future (propose, don't build without sign-off)
- Swap matplotlib for a web/canvas renderer (a local WebSocket bridge from the Python data layer to a browser frontend; Lightweight Charts v5 with custom heatmap primitives is a known-good approach). Keep the Python data layer unchanged.
- Multi-symbol support.
- Liquidity-wall / iceberg detection on top of the parsed book.

---

## 6. How to run / environment

- Working directory: this folder (`ovultor`).
- `.env` keys (already configured): `SCHWAB_API_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_CALLBACK_URL` (with trailing slash), `SCHWAB_ACCOUNT_ID`, `SCHWAB_TOKEN_PATH`, `SCHWAB_SYMBOL`.
- Run offline (no creds): `python schwab_orderflow_heatmap.py --simulate`
- Run live: `python schwab_orderflow_heatmap.py` (reads `.env`), optionally `--symbol AAPL`, `--raw` for a one-shot frame dump.
- You **cannot** test the live path yourself (no creds, market hours). Lean on `--simulate` and the captured frame fixture for all verification; call out anything that genuinely needs a live session for the owner to check.
- Harmless noise: `MESA-INTEL ... FINISHME` lines are Intel GPU/Vulkan warnings from the matplotlib window — ignore them, don't try to "fix" them.

---

## 7. Working style

The owner is a strong technical builder who owns discretionary decisions. Be concise and technical, show tradeoffs rather than hand-waving, and don't over-engineer. Propose a short plan before each phase and confirm the boundary before proceeding. Keep changes reviewable (small, coherent commits per phase). Preserve the working seed's behavior except where a phase explicitly changes it. Avoid filler prose in code comments and docs — explain the non-obvious, skip the obvious.
