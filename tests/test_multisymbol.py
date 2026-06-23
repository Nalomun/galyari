"""Per-symbol shared state + multi-entry book parsing (Phase 5A Tier 2)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state  # noqa: E402
from orderbook import Level, Trade, parse_books  # noqa: E402


def setup_function(_):
    state.reset()


def _entry(sym, mid):
    return {"key": sym, "BOOK_TIME": 0,
            "BIDS": [{"BID_PRICE": mid - 0.01, "TOTAL_VOLUME": 100}],
            "ASKS": [{"ASK_PRICE": mid + 0.01, "TOTAL_VOLUME": 100}]}


def test_parse_books_returns_one_per_symbol():
    msg = {"content": [_entry("AAPL", 200.0), _entry("GOOG", 150.0)]}
    books = parse_books(msg)
    assert {b.symbol for b in books} == {"AAPL", "GOOG"}
    assert {round(b.mid, 2) for b in books} == {200.0, 150.0}


def _book(sym, mid):
    from orderbook import Book
    return Book(sym, 0, bids=(Level(mid - 0.01, 100),), asks=(Level(mid + 0.01, 100),))


def test_subscribed_set_tracks_many_and_guards_foreign():
    state.set_subscribed(["AAPL", "GOOG", "TSLA"], focus="AAPL")
    for sym, mid in (("AAPL", 200), ("GOOG", 150), ("TSLA", 250)):
        state.set_book(_book(sym, mid))
    state.set_book(_book("NVDA", 900))                 # not subscribed -> dropped
    books = state.get_books()
    assert set(books) == {"AAPL", "GOOG", "TSLA"}
    assert state.get_book().symbol == "AAPL"           # no-arg returns the focus
    assert state.get_book("GOOG").mid == 150.0


def test_set_focus_is_free_switch_no_data_loss():
    state.set_subscribed(["AAPL", "GOOG"], focus="AAPL")
    state.set_book(_book("AAPL", 200))
    state.set_book(_book("GOOG", 150))
    state.set_focus("GOOG")                            # switch focus only
    assert state.get_book().symbol == "GOOG"
    assert state.get_book("AAPL").mid == 200.0         # AAPL data still present


def test_add_subscription_admits_new_symbol():
    state.set_subscribed(["AAPL"], focus="AAPL")
    state.set_book(_book("MSFT", 400))                 # dropped before subscribing
    assert "MSFT" not in state.get_books()
    state.add_subscription("MSFT")
    state.set_book(_book("MSFT", 400))                 # now admitted
    assert state.get_book("MSFT").mid == 400.0


def test_per_symbol_trade_buffers_drain_independently():
    state.set_subscribed(["AAPL", "GOOG"], focus="AAPL")
    state.add_trade(Trade("AAPL", 0, 200.0, 100, 1))
    state.add_trade(Trade("GOOG", 0, 150.0, 300, -1))
    assert [t.symbol for t in state.drain_trades("AAPL")] == ["AAPL"]
    assert state.drain_trades("AAPL") == []            # drained
    assert [t.symbol for t in state.drain_trades("GOOG")] == ["GOOG"]


import asyncio  # noqa: E402
import feeds  # noqa: E402


class _FakeStream:
    def __init__(self): self.calls = []
    async def nasdaq_book_subs(self, syms): self.calls.append(("book_sub", syms))
    async def nasdaq_book_unsubs(self, syms): self.calls.append(("book_unsub", syms))
    async def level_one_equity_subs(self, syms): self.calls.append(("l1_sub", syms))
    async def level_one_equity_unsubs(self, syms): self.calls.append(("l1_unsub", syms))


def test_multi_switch_to_subscribed_is_focus_only_no_socket():
    state.set_subscribed(["AAPL", "GOOG"], focus="AAPL")
    fs = _FakeStream()
    sub = {"AAPL", "GOOG"}
    focus = asyncio.run(feeds._apply_switch(fs, sub, "AAPL", "GOOG", trades=True, multi=True))
    assert focus == "GOOG" and state.get_focus() == "GOOG"
    assert fs.calls == []                              # no subscribe/unsubscribe at all


def test_multi_switch_to_new_symbol_subscribes_and_admits():
    state.set_subscribed(["AAPL"], focus="AAPL")
    fs = _FakeStream()
    sub = {"AAPL"}
    focus = asyncio.run(feeds._apply_switch(fs, sub, "AAPL", "TSLA", trades=True, multi=True))
    assert focus == "TSLA" and "TSLA" in sub
    assert ("book_sub", ["TSLA"]) in fs.calls and ("l1_sub", ["TSLA"]) in fs.calls
    state.set_book(_book("TSLA", 250))                 # guard now admits it
    assert state.get_book("TSLA").mid == 250.0


def test_single_symbol_switch_still_resubscribes():
    state.set_active_symbol("AAPL")
    fs = _FakeStream()
    focus = asyncio.run(feeds._apply_switch(fs, {"AAPL"}, "AAPL", "GOOG",
                                            trades=True, multi=False))
    assert focus == "GOOG"
    assert ("book_unsub", ["AAPL"]) in fs.calls and ("book_sub", ["GOOG"]) in fs.calls
