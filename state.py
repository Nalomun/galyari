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


def set_book(book: Book | None) -> None:
    """Replace the latest book (full-snapshot semantics)."""
    global _book
    with _lock:
        _book = book


def get_book() -> Book | None:
    """Return the latest book snapshot (may be None — a valid 'no data yet' state)."""
    with _lock:
        return _book


def add_trade(trade: Trade) -> None:
    with _lock:
        _trades.append(trade)


def drain_trades() -> list[Trade]:
    """Return and clear all buffered trades since the last call."""
    with _lock:
        out = list(_trades)
        _trades.clear()
        return out


def reset() -> None:
    """Clear all shared state (used by tests and at replay start)."""
    global _book
    with _lock:
        _book = None
        _trades.clear()
