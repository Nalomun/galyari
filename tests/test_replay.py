"""Recorder → replay round-trip and live-message recording, all offline."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state  # noqa: E402
import feeds  # noqa: E402
from orderbook import Book, Level, Trade  # noqa: E402
from recorder import Recorder  # noqa: E402


def test_sim_record_replay_roundtrip(tmp_path):
    path = tmp_path / "tape.jsonl"
    r = Recorder(str(path))
    book = Book("SIM", 1000, bids=(Level(99.99, 400),), asks=(Level(100.01, 250),))
    r.write_sim_book(book)
    r.write_trade(Trade("SIM", 1001, 100.01, 300, 1))
    r.write_sim_book(Book("SIM", 1010, bids=(Level(99.98, 100),), asks=(Level(100.02, 100),)))
    r.close()

    state.reset()
    feeds.run_replay(str(path), speed=1000.0)   # high speed → ~no sleeping
    b = state.get_book()
    assert b is not None and b.ts_ms == 1010
    assert b.best_bid.price == 99.98
    trades = state.drain_trades()
    assert len(trades) == 1 and trades[0].size == 300 and trades[0].side == 1


def test_record_live_book_and_replay(tmp_path):
    path = tmp_path / "live.jsonl"
    r = Recorder(str(path))
    msg = {"content": [{"key": "GOOG", "BOOK_TIME": 1782144614895,
                        "BIDS": [{"BID_PRICE": 344.80, "TOTAL_VOLUME": 120, "BIDS": []}],
                        "ASKS": [{"ASK_PRICE": 344.90, "TOTAL_VOLUME": 100, "ASKS": []}]}]}
    r.write_book(msg)
    r.close()

    state.reset()
    feeds.run_replay(str(path), speed=1000.0)
    b = state.get_book()
    assert b is not None and b.symbol == "GOOG"
    assert b.best_bid.price == 344.80 and b.best_ask.price == 344.90


def test_replay_ignores_blank_and_bad_lines(tmp_path):
    path = tmp_path / "messy.jsonl"
    path.write_text('\n{"t":"sim_book","ts_ms":5,"symbol":"SIM",'
                    '"bids":[[10.0,5]],"asks":[[10.1,5]]}\nnot json\n\n')
    state.reset()
    feeds.run_replay(str(path), speed=1000.0)
    assert state.get_book().best_bid.price == 10.0
