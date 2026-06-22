"""Unit tests for the parsing/book-state core, driven by the captured GOOG frame."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orderbook import (  # noqa: E402
    Book, Level, OrderBook, Trade, infer_side, parse_nasdaq_book,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "goog_book.json")


@pytest.fixture
def goog_msg():
    with open(FIXTURE) as f:
        return json.load(f)


def test_parses_symbol_and_time(goog_msg):
    book = parse_nasdaq_book(goog_msg)
    assert book.symbol == "GOOG"
    assert book.ts_ms == 1782144614895


def test_level_counts_and_prices(goog_msg):
    book = parse_nasdaq_book(goog_msg)
    assert [l.price for l in book.bids] == [344.80, 344.67]
    assert [l.price for l in book.asks] == [344.90]


def test_total_volume_aggregation(goog_msg):
    book = parse_nasdaq_book(goog_msg)
    # level 2 aggregates edgx(120) + batx(80) = 200 across two venues
    lvl = book.bids[1]
    assert lvl.volume == 200
    assert len(lvl.venues) == 2
    assert {v.exchange for v in lvl.venues} == {"edgx", "batx"}
    assert sum(v.volume for v in lvl.venues) == lvl.volume


def test_best_and_mid_and_spread(goog_msg):
    book = parse_nasdaq_book(goog_msg)
    assert book.best_bid.price == 344.80
    assert book.best_ask.price == 344.90
    assert book.mid == pytest.approx(344.85)
    assert book.spread == pytest.approx(0.10)


def test_bids_sorted_high_to_low_even_if_input_unsorted():
    msg = {"content": [{
        "key": "X", "BOOK_TIME": 1,
        "BIDS": [
            {"BID_PRICE": 10.0, "TOTAL_VOLUME": 5, "BIDS": []},
            {"BID_PRICE": 11.0, "TOTAL_VOLUME": 5, "BIDS": []},
        ],
        "ASKS": [
            {"ASK_PRICE": 13.0, "TOTAL_VOLUME": 5, "ASKS": []},
            {"ASK_PRICE": 12.0, "TOTAL_VOLUME": 5, "ASKS": []},
        ],
    }]}
    book = parse_nasdaq_book(msg)
    assert book.best_bid.price == 11.0      # highest bid first
    assert book.best_ask.price == 12.0      # lowest ask first


def test_empty_and_contentless_frames_return_none():
    assert parse_nasdaq_book({"content": []}) is None
    assert parse_nasdaq_book({}) is None
    assert parse_nasdaq_book({"content": [{"key": "X", "BOOK_TIME": 1,
                                           "BIDS": [], "ASKS": []}]}) is None


def test_levels_iterator_covers_both_sides(goog_msg):
    book = parse_nasdaq_book(goog_msg)
    pairs = list(book.levels())
    assert (344.80, 120) in pairs
    assert (344.90, 100) in pairs
    assert len(pairs) == 3


def test_orderbook_full_replace(goog_msg):
    ob = OrderBook()
    assert ob.apply(goog_msg) is not None
    assert ob.frames_seen == 1
    # an empty frame does not clobber a good book, and is not counted
    assert ob.apply({"content": []}) is None
    assert ob.frames_seen == 1
    assert ob.book.symbol == "GOOG"
    # a second good frame replaces wholesale
    msg2 = {"content": [{"key": "GOOG", "BOOK_TIME": 2,
                         "BIDS": [{"BID_PRICE": 1.0, "TOTAL_VOLUME": 9, "BIDS": []}],
                         "ASKS": []}]}
    ob.apply(msg2)
    assert ob.frames_seen == 2
    assert ob.book.ts_ms == 2
    assert len(ob.book.bids) == 1


def test_malformed_level_is_skipped_not_fatal():
    msg = {"content": [{"key": "X", "BOOK_TIME": 1,
                        "BIDS": [
                            {"TOTAL_VOLUME": 5, "BIDS": []},        # missing price
                            {"BID_PRICE": 9.0, "TOTAL_VOLUME": 7, "BIDS": []},
                        ], "ASKS": []}]}
    book = parse_nasdaq_book(msg)
    assert len(book.bids) == 1
    assert book.bids[0].price == 9.0


def test_infer_side(goog_msg):
    book = parse_nasdaq_book(goog_msg)        # bid 344.80 / ask 344.90
    assert infer_side(344.90, book) == 1      # at ask -> buy
    assert infer_side(344.95, book) == 1      # above ask -> buy
    assert infer_side(344.80, book) == -1     # at bid -> sell
    assert infer_side(344.70, book) == -1     # below bid -> sell
    assert infer_side(344.85, book) == 0      # mid -> unknown
    assert infer_side(344.85, None) == 0


def test_trade_dataclass_defaults():
    t = Trade(symbol="GOOG", ts_ms=1, price=344.9, size=100)
    assert t.side == 0
