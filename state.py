"""Thread-safe shared store between a producer thread and the render loop.

Producers (Schwab stream / simulator / replay) write; the matplotlib main loop
reads. Only the *latest* book is kept, plus a bounded buffer of recent trades that
the renderer drains each tick. No history is kept here — history lives in the
renderer's rolling matrix and (optionally) the recording file.

matplotlib-free on purpose: this is part of the producer/renderer seam.
"""

from __future__ import annotations

import threading
from collections import deque

from orderbook import Book, Trade

_lock = threading.Lock()
_book: Book | None = None
_trades: deque[Trade] = deque(maxlen=2000)
# When set (live mode), frames whose symbol != active are dropped. This is what
# keeps a stale book/print from the *previous* symbol off the screen during the
# brief window between a hot-switch and the old subscription actually going quiet.
# None (sim/replay) means "accept everything".
_active_symbol: str | None = None


def set_book(book: Book | None) -> None:
    """Replace the latest book (full-snapshot semantics).

    A book for a symbol other than the active one is ignored (post-switch guard).
    """
    global _book
    with _lock:
        if (book is not None and _active_symbol is not None
                and book.symbol != _active_symbol):
            return
        _book = book


def get_book() -> Book | None:
    """Return the latest book snapshot (may be None — a valid 'no data yet' state)."""
    with _lock:
        return _book


def set_active_symbol(symbol: str | None) -> None:
    """Set the symbol the renderer currently expects (enables the stale-frame guard)."""
    global _active_symbol
    with _lock:
        _active_symbol = symbol


def get_active_symbol() -> str | None:
    with _lock:
        return _active_symbol


def clear_for_symbol(symbol: str) -> None:
    """Atomically drop book + trades and arm the guard for `symbol` (hot switch)."""
    global _book, _active_symbol
    with _lock:
        _book = None
        _trades.clear()
        _active_symbol = symbol


def add_trade(trade: Trade) -> None:
    with _lock:
        if _active_symbol is not None and trade.symbol != _active_symbol:
            return
        _trades.append(trade)


def drain_trades() -> list[Trade]:
    """Return and clear all buffered trades since the last call."""
    with _lock:
        out = list(_trades)
        _trades.clear()
        return out


def reset() -> None:
    """Clear all shared state (used by tests and at replay start)."""
    global _book, _active_symbol
    with _lock:
        _book = None
        _trades.clear()
        _active_symbol = None
