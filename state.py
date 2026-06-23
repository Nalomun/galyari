"""Thread-safe shared store between a producer thread and the render loop.

Producers (Schwab stream / simulator / replay) write; a renderer (matplotlib or the
WebSocket bridge) reads. Only the *latest* book per symbol is kept, plus a bounded
buffer of recent trades per symbol that the renderer drains each tick. No history is
kept here — history lives in the renderer's rolling matrix and (optionally) the
recording file.

The store is **per-symbol** (Phase 5A Tier 2) so several books can be tracked at once,
but the single-symbol API is preserved verbatim: the no-argument `get_book()` /
`drain_trades()` return the *focused* symbol, and `set_active_symbol` / `clear_for_symbol`
behave exactly as in the single-symbol era. Multi-symbol callers use `set_subscribed`,
`set_focus`, and `get_books`.

matplotlib-free on purpose: this is part of the producer/renderer seam.
"""

from __future__ import annotations

import threading
from collections import deque

from orderbook import Book, Trade

_TRADE_MAX = 2000

_lock = threading.Lock()
_books: dict[str, Book] = {}
_trades: dict[str, deque[Trade]] = {}
# The guard set: when not None (live), frames whose symbol isn't in it are dropped —
# this keeps stale/foreign frames off the screen. A *set* (not a single symbol) is what
# lets several symbols stream at once. None (sim/replay) means "accept everything".
_subscribed: set[str] | None = None
# The symbol the no-arg getters return — the matplotlib view, or the browser's main panel.
_focus: str | None = None


def _dq(symbol: str) -> deque[Trade]:
    dq = _trades.get(symbol)
    if dq is None:
        dq = _trades[symbol] = deque(maxlen=_TRADE_MAX)
    return dq


# --- writers (producers) -----------------------------------------------------
def set_book(book: Book | None) -> None:
    """Store the latest book for its symbol (full-snapshot semantics).

    Dropped if a guard set is armed and the symbol isn't in it (post-switch / foreign).
    The first symbol seen auto-focuses, so sim/replay need no focus ceremony.
    """
    global _focus
    if book is None:
        return
    with _lock:
        if _subscribed is not None and book.symbol not in _subscribed:
            return
        if _focus is None:
            _focus = book.symbol
        _books[book.symbol] = book


def add_trade(trade: Trade) -> None:
    with _lock:
        if _subscribed is not None and trade.symbol not in _subscribed:
            return
        _dq(trade.symbol).append(trade)


# --- readers (render loop) ---------------------------------------------------
def get_book(symbol: str | None = None) -> Book | None:
    """Latest book for `symbol`, or the focused symbol when omitted (may be None)."""
    with _lock:
        sym = symbol if symbol is not None else _focus
        return _books.get(sym) if sym is not None else None


def get_books() -> dict[str, Book]:
    """Snapshot of every tracked symbol's latest book (for multi-symbol sidebars)."""
    with _lock:
        return dict(_books)


def drain_trades(symbol: str | None = None) -> list[Trade]:
    """Return and clear buffered trades for `symbol` (focused symbol when omitted)."""
    with _lock:
        sym = symbol if symbol is not None else _focus
        dq = _trades.get(sym)
        if not dq:
            return []
        out = list(dq)
        dq.clear()
        return out


# --- focus & subscription control --------------------------------------------
def set_focus(symbol: str | None) -> None:
    """Choose which symbol the no-arg getters return. Cheap; no resubscribe needed when
    the symbol is already in the subscription set (multi-symbol panel switch)."""
    global _focus
    with _lock:
        _focus = symbol


def get_focus() -> str | None:
    with _lock:
        return _focus


def set_subscribed(symbols, focus: str | None = None) -> None:
    """Arm the guard to a *set* of symbols (multi-symbol live) and clear prior state.
    Focus defaults to the first symbol given."""
    global _subscribed, _focus
    syms = list(symbols)
    with _lock:
        _books.clear()
        _trades.clear()
        _subscribed = set(syms)
        _focus = focus if focus is not None else (syms[0] if syms else None)


def add_subscription(symbol: str) -> None:
    """Add one symbol to the guard set (e.g. a symbol typed in the UI that wasn't in the
    watchlist). No-op on the accept-all (sim/replay) guard."""
    global _subscribed
    with _lock:
        if _subscribed is not None:
            _subscribed.add(symbol)


def get_subscribed() -> set[str] | None:
    with _lock:
        return set(_subscribed) if _subscribed is not None else None


# --- single-symbol shims (preserved verbatim for the matplotlib path) --------
def set_active_symbol(symbol: str | None) -> None:
    """Single-symbol guard: track exactly `symbol` (or accept-all when None)."""
    global _subscribed, _focus
    with _lock:
        _subscribed = {symbol} if symbol is not None else None
        _focus = symbol


def get_active_symbol() -> str | None:
    with _lock:
        return _focus


def clear_for_symbol(symbol: str) -> None:
    """Atomically drop all books + trades and arm the guard for `symbol` (hot switch)."""
    global _subscribed, _focus
    with _lock:
        _books.clear()
        _trades.clear()
        _subscribed = {symbol}
        _focus = symbol


def reset() -> None:
    """Clear all shared state (used by tests and at replay start)."""
    global _subscribed, _focus
    with _lock:
        _books.clear()
        _trades.clear()
        _subscribed = None
        _focus = None
