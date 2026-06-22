# Data Schema

## `NASDAQ_BOOK` wire message (as relabeled by schwab-py)

schwab-py relabels the raw numeric streamer fields to readable names. Each message has a
`content` list with one entry per subscribed symbol. A real captured GOOG frame (trimmed),
used verbatim as the test fixture in `tests/fixtures/goog_book.json`:

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

### Field reference

| Field | Where | Meaning |
|-------|-------|---------|
| `key` | entry | symbol |
| `BOOK_TIME` | entry | book timestamp, **epoch milliseconds** |
| `BIDS` / `ASKS` | entry | array of **price levels**, sorted best-to-worse |
| `BID_PRICE` / `ASK_PRICE` | level | price of the level |
| `TOTAL_VOLUME` | level | size aggregated across all venues at that price |
| `NUM_BIDS` / `NUM_ASKS` | level | count of venue quotes at that price |
| `BIDS` / `ASKS` (nested) | level | per-venue breakdown |
| `EXCHANGE` | venue | venue code (`NSDQ`, `arcx`, `edgx`, `batx`, `nyse`, `memx`, `miax`, …) |
| `BID_VOLUME` / `ASK_VOLUME` | venue | size at that venue |
| `SEQUENCE` | venue | venue update sequence number |

Notes:

- This is a **multi-venue aggregated book** surfaced through Schwab, not Nasdaq-only — the
  observed venues span NYSE/MEMX/MIAX/etc. The per-venue breakdown is preserved on the
  parsed `Level` (`venues`) even though the heatmap currently only uses `TOTAL_VOLUME`; it
  may matter for research (e.g. venue-specific persistence).
- Field-name parsing was confirmed correct against the live frame above; the parser does
  not need re-deriving.

## Snapshot vs. delta

**Finding: each `NASDAQ_BOOK` message is a full book snapshot, not an incremental delta.**

The `BIDS`/`ASKS` arrays in a single message represent the **complete current visible
book** at `BOOK_TIME`. There is no add/modify/delete keying, no per-level update flag, and
no sequence reconciliation across messages at the top level — the only `SEQUENCE` numbers
are per-venue identifiers inside a level, not a stream-wide delta cursor. This matches the
behavior Schwab inherited from the TD Ameritrade streamer's book services, and it is why
the **full-replace model is correct and simpler**: on each message we discard the previous
book and rebuild from the new arrays.

> Confidence: high, but **not live-verified in this session** (no credentials / market
> closed at authoring time). What would falsify it: levels that "stick" stale across
> frames, volumes that only ever grow, or messages that carry a single changed level. If
> you observe any of those during a live session, revisit `OrderBook.apply()` to support
> deltas. Until then, full-replace stands.

## Parsed structures (`orderbook.py`)

The wire message is parsed into these typed, matplotlib-free structures:

```python
@dataclass(frozen=True)
class Venue:
    exchange: str
    volume: int
    sequence: int

@dataclass(frozen=True)
class Level:
    price: float
    volume: int          # TOTAL_VOLUME, aggregated across venues
    venues: tuple[Venue, ...]   # per-venue breakdown (may be empty)

@dataclass(frozen=True)
class Book:
    symbol: str
    ts_ms: int           # BOOK_TIME, epoch milliseconds
    bids: tuple[Level, ...]   # best (highest) first
    asks: tuple[Level, ...]   # best (lowest) first

    # convenience: best_bid, best_ask, mid, spread
```

`parse_nasdaq_book(msg) -> Book | None` returns `None` for an empty/contentless frame
(valid, not an error). `OrderBook` is a thin stateful holder whose `apply(msg)` parses and
replaces its current `Book` (full-replace, per the finding above).

### Trades (`TIMESALE_EQUITY`)

The trades layer parses Schwab's `TIMESALE_EQUITY` stream into:

```python
@dataclass(frozen=True)
class Trade:
    symbol: str
    ts_ms: int
    price: float
    size: int
    side: int   # +1 buyer-initiated (>= ask), -1 seller-initiated (<= bid), 0 unknown
```

Aggressor side is inferred against the prevailing book (trade at/above best ask = buy;
at/below best bid = sell), since the stream does not label aggressor directly. In simulate
and replay this is synthesized/recorded so the layer is testable offline.
