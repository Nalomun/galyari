# ovultor — Live Equity Order-Flow Heatmap

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

### Useful flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--simulate` | off | synthetic book, no credentials |
| `--symbol SYM` | `$SCHWAB_SYMBOL` or `GOOG` | symbol to subscribe |
| `--tick T` | `0.01` | price bin size (dollars) |
| `--rows N` | `120` | number of price bins shown |
| `--cols N` | `240` | number of time columns (history width) |
| `--raw` | off | dump one raw book frame then continue |

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

See [ARCHITECTURE.md](ARCHITECTURE.md#roadmap) for the phase roadmap. Implemented:
parsing/book-state core, recentering auto-zoom heatmap, trades layer (T&S bubbles + CVD),
and record/replay.
