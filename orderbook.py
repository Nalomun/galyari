"""Reusable order-book core for the Schwab NASDAQ_BOOK stream.

This is the *stable seam* other research projects import. It is intentionally
dependency-light (stdlib only — no numpy, no matplotlib) so it can be pulled into
feature pipelines without dragging in a renderer.

Parsing is full-replace: each NASDAQ_BOOK message is treated as a complete book
snapshot (see DATA_SCHEMA.md, "Snapshot vs. delta"). `parse_nasdaq_book` turns one
schwab-py message into an immutable `Book`; `OrderBook` is a thin stateful holder.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- schwab-py field labels (confirmed against a live GOOG frame) -------------
BIDS_KEY, ASKS_KEY = "BIDS", "ASKS"
BID_PRICE_KEY, ASK_PRICE_KEY = "BID_PRICE", "ASK_PRICE"
VOLUME_KEY = "TOTAL_VOLUME"
BOOK_TIME_KEY = "BOOK_TIME"
SYMBOL_KEY = "key"
# nested per-venue level fields
EXCHANGE_KEY = "EXCHANGE"
BID_VOL_KEY, ASK_VOL_KEY = "BID_VOLUME", "ASK_VOLUME"
SEQUENCE_KEY = "SEQUENCE"


@dataclass(frozen=True)
class Venue:
    """One venue's quote inside a price level."""
    exchange: str
    volume: int
    sequence: int


@dataclass(frozen=True)
class Level:
    """A single price level, aggregated across venues."""
    price: float
    volume: int  # TOTAL_VOLUME across venues at this price
    venues: tuple[Venue, ...] = ()


@dataclass(frozen=True)
class Book:
    """Immutable snapshot of the visible book at a point in time.

    bids are best (highest) first; asks are best (lowest) first.
    """
    symbol: str
    ts_ms: int
    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb.price + ba.price) / 2.0

    @property
    def spread(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return ba.price - bb.price

    def levels(self):
        """Iterate all (price, volume) on both sides — convenience for binning."""
        for lvl in self.bids:
            yield lvl.price, lvl.volume
        for lvl in self.asks:
            yield lvl.price, lvl.volume


def _parse_venues(raw_level: dict, side_key: str, vol_key: str) -> tuple[Venue, ...]:
    out = []
    for v in raw_level.get(side_key, ()) or ():
        try:
            out.append(Venue(
                exchange=str(v.get(EXCHANGE_KEY, "")),
                volume=int(v.get(vol_key, 0)),
                sequence=int(v.get(SEQUENCE_KEY, 0)),
            ))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def _parse_levels(raw_levels, price_key, side_key, vol_key) -> tuple[Level, ...]:
    out = []
    for lvl in raw_levels or ():
        try:
            price = float(lvl[price_key])
            volume = int(lvl.get(VOLUME_KEY, 0))
        except (KeyError, TypeError, ValueError):
            continue
        out.append(Level(
            price=price,
            volume=volume,
            venues=_parse_venues(lvl, side_key, vol_key),
        ))
    return tuple(out)


def parse_entry(entry: dict) -> Book:
    """Parse a single per-symbol content entry into a Book.

    Levels are sorted defensively (bids high→low, asks low→high) so downstream
    `best_bid`/`best_ask` are correct even if a feed ever delivers them unsorted.
    """
    symbol = str(entry.get(SYMBOL_KEY, ""))
    ts_ms = int(entry.get(BOOK_TIME_KEY, 0) or 0)
    bids = _parse_levels(entry.get(BIDS_KEY), BID_PRICE_KEY, BIDS_KEY, BID_VOL_KEY)
    asks = _parse_levels(entry.get(ASKS_KEY), ASK_PRICE_KEY, ASKS_KEY, ASK_VOL_KEY)
    bids = tuple(sorted(bids, key=lambda l: l.price, reverse=True))
    asks = tuple(sorted(asks, key=lambda l: l.price))
    return Book(symbol=symbol, ts_ms=ts_ms, bids=bids, asks=asks)


def parse_nasdaq_book(msg: dict) -> Book | None:
    """schwab-py NASDAQ_BOOK message -> Book, or None for an empty/contentless frame.

    An empty frame is a valid state (market closed / no entitlement), never an error.
    Returns only the first content entry — see `parse_books` for the multi-symbol form.
    """
    content = (msg or {}).get("content") or []
    if not content:
        return None
    book = parse_entry(content[0])
    if not book.bids and not book.asks:
        return None
    return book


def parse_books(msg: dict) -> list[Book]:
    """Parse *every* per-symbol content entry into a list of Books (Phase 5A Tier 2).

    One NASDAQ_BOOK message can carry one entry per subscribed symbol; the single-symbol
    `parse_nasdaq_book` keeps only the first. Empty (no-level) entries are skipped.
    """
    out = []
    for entry in (msg or {}).get("content") or []:
        book = parse_entry(entry)
        if book.bids or book.asks:
            out.append(book)
    return out


@dataclass(frozen=True)
class Trade:
    """One time-and-sales print with inferred aggressor side."""
    symbol: str
    ts_ms: int
    price: float
    size: int
    side: int = 0  # +1 buyer-initiated, -1 seller-initiated, 0 unknown


def infer_side(price: float, book: Book | None) -> int:
    """Classify a trade's aggressor against the prevailing book.

    >= best ask  -> +1 (buyer lifted the offer)
    <= best bid  -> -1 (seller hit the bid)
    otherwise     ->  0 (mid / unknown)
    """
    if book is None:
        return 0
    ba, bb = book.best_ask, book.best_bid
    if ba is not None and price >= ba.price:
        return 1
    if bb is not None and price <= bb.price:
        return -1
    return 0


class OrderBook:
    """Stateful holder of the latest book (full-replace semantics).

    Not thread-safe by itself; cross-thread sharing goes through state.py.
    """

    def __init__(self) -> None:
        self.book: Book | None = None
        self.frames_seen: int = 0

    def apply(self, msg: dict) -> Book | None:
        """Parse a message and replace current book. Returns the new Book or None."""
        book = parse_nasdaq_book(msg)
        if book is not None:
            self.book = book
            self.frames_seen += 1
        return book

    @property
    def mid(self) -> float | None:
        return self.book.mid if self.book else None
