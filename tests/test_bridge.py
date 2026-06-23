"""Bridge frame serialization (Phase 5B/5A.2). Pure — no socket, no producer thread;
drives the real `state` store the push loop reads from. Frames carry every watched
symbol in full so the browser can render a grid."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge  # noqa: E402
import state  # noqa: E402
from orderbook import Book, Level, Trade  # noqa: E402


def setup_function(_):
    state.reset()


def _book(sym="AAPL", mid=100.0):
    return Book(sym, 1700000000000,
                bids=(Level(mid - 0.01, 300), Level(mid - 0.02, 700)),
                asks=(Level(mid + 0.01, 200), Level(mid + 0.02, 500)))


def _rt(subscribed=("AAPL",), multi=False):
    return {"focus": subscribed[0] if subscribed else None, "tick": 0.01, "cols": 240,
            "multi": multi, "subscribed": list(subscribed), "cvd": {}, "delta": {}}


def test_frame_carries_every_symbol_in_full():
    state.set_subscribed(["AAPL", "GOOG"], focus="AAPL")
    state.set_book(_book("AAPL", 200))
    state.set_book(_book("GOOG", 150))
    msg = json.loads(bridge._build_message(_rt(("AAPL", "GOOG"), multi=True)))
    assert msg["type"] == "frame" and msg["order"] == ["AAPL", "GOOG"]
    assert set(msg["symbols"]) == {"AAPL", "GOOG"}
    a = msg["symbols"]["AAPL"]
    assert a["bids"][0] == [199.99, 300] and a["asks"][0] == [200.01, 200]
    assert a["mid"] == 200.0 and round(a["spread"], 2) == 0.02


def test_cvd_accumulates_per_symbol_and_delta_resets():
    state.set_subscribed(["AAPL", "GOOG"], focus="AAPL")
    state.set_book(_book("AAPL", 200)); state.set_book(_book("GOOG", 150))
    rt = _rt(("AAPL", "GOOG"), multi=True)
    state.add_trade(Trade("AAPL", 0, 200.01, 200, 1))
    state.add_trade(Trade("AAPL", 0, 199.99, 500, -1))
    state.add_trade(Trade("GOOG", 0, 150.01, 100, 1))
    m1 = json.loads(bridge._build_message(rt))
    assert m1["symbols"]["AAPL"]["cvd"] == -300 and m1["symbols"]["AAPL"]["delta"] == -300
    assert m1["symbols"]["GOOG"]["cvd"] == 100        # independent per symbol
    m2 = json.loads(bridge._build_message(rt))         # no new prints
    assert m2["symbols"]["AAPL"]["cvd"] == -300 and m2["symbols"]["AAPL"]["delta"] == 0


def test_imbalance_sign_and_range():
    imb = bridge._imbalance(_book())
    assert -1 <= imb <= 1 and imb > 0                  # bids 1000 > asks 700


def test_empty_book_symbol_is_valid():
    state.set_subscribed(["AAPL"], focus="AAPL")        # subscribed but no book yet
    msg = json.loads(bridge._build_message(_rt()))
    assert msg["symbols"]["AAPL"]["bids"] == [] and msg["symbols"]["AAPL"]["mid"] is None


def test_sim_order_falls_back_to_books_when_unsubscribed():
    state.set_book(_book("SIM", 100))                   # sim: no subscribed set
    msg = json.loads(bridge._build_message(_rt(subscribed=())))
    assert msg["order"] == ["SIM"] and "SIM" in msg["symbols"]
