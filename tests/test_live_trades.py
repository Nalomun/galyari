"""Deriving prints from LEVEL_ONE_EQUITY deltas (the live trades path)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state  # noqa: E402
import feeds  # noqa: E402
from orderbook import Book, Level  # noqa: E402


def _l1(symbol="GOOG", **fields):
    return {"content": [{"key": symbol, **fields}]}


def setup_function(_):
    state.reset()
    feeds._last_trade.clear()
    # prevailing book so aggressor side can be inferred
    state.set_book(Book("GOOG", 0, bids=(Level(344.80, 100),), asks=(Level(344.90, 100),)))


def test_first_message_sets_baseline_no_trade():
    feeds._on_level_one(_l1(TRADE_TIME_MILLIS=1000, LAST_PRICE=344.90, LAST_SIZE=100))
    assert state.drain_trades() == []          # first sighting establishes baseline only


def test_advancing_trade_time_emits_print_with_side():
    feeds._on_level_one(_l1(TRADE_TIME_MILLIS=1000, LAST_PRICE=344.90, LAST_SIZE=100))
    feeds._on_level_one(_l1(TRADE_TIME_MILLIS=1001, LAST_PRICE=344.90, LAST_SIZE=200))
    trades = state.drain_trades()
    assert len(trades) == 1
    assert trades[0].size == 200 and trades[0].price == 344.90
    assert trades[0].side == 1                 # at ask -> buyer-initiated


def test_delta_carries_forward_missing_fields():
    feeds._on_level_one(_l1(TRADE_TIME_MILLIS=1000, LAST_PRICE=344.80, LAST_SIZE=100))
    # next delta only advances the time; price/size must carry forward
    feeds._on_level_one(_l1(TRADE_TIME_MILLIS=1002))
    trades = state.drain_trades()
    assert len(trades) == 1
    assert trades[0].price == 344.80 and trades[0].size == 100
    assert trades[0].side == -1                # at bid -> seller-initiated


def test_no_emit_when_nothing_changed():
    feeds._on_level_one(_l1(TRADE_TIME_MILLIS=1000, LAST_PRICE=344.90, LAST_SIZE=100))
    feeds._on_level_one(_l1(QUOTE_TIME_MILLIS=1500))   # quote update, no new trade
    assert state.drain_trades() == []
