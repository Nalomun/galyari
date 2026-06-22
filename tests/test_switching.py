"""Hot ticker switching: shared-state guard + the async resubscribe path."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state  # noqa: E402
import feeds  # noqa: E402
from orderbook import Book, Level  # noqa: E402
from schwab_orderflow_heatmap import Heatmap  # noqa: E402


def setup_function(_):
    state.reset()
    feeds._last_trade.clear()


def _book(sym, mid=100.0):
    return Book(sym, 0, bids=(Level(mid - 0.01, 100),), asks=(Level(mid + 0.01, 100),))


def test_active_symbol_guard_drops_foreign_frames():
    state.set_active_symbol("AAPL")
    state.set_book(_book("GOOG"))          # stale frame from old symbol
    assert state.get_book() is None
    state.set_book(_book("AAPL"))          # current symbol accepted
    assert state.get_book().symbol == "AAPL"


def test_guard_off_when_no_active_symbol():
    state.set_book(_book("SIM"))           # sim/replay: accept everything
    assert state.get_book().symbol == "SIM"


def test_clear_for_symbol_resets_and_arms_guard():
    state.set_book(_book("GOOG"))
    state.clear_for_symbol("TSLA")
    assert state.get_book() is None
    assert state.get_active_symbol() == "TSLA"
    state.set_book(_book("GOOG"))           # old symbol now rejected
    assert state.get_book() is None


class FakeStream:
    """Records subscribe/unsubscribe calls so we can assert the switch sequence."""
    def __init__(self):
        self.calls = []

    async def nasdaq_book_unsubs(self, syms): self.calls.append(("book_unsub", syms))
    async def nasdaq_book_subs(self, syms): self.calls.append(("book_sub", syms))
    async def level_one_equity_unsubs(self, syms): self.calls.append(("l1_unsub", syms))
    async def level_one_equity_subs(self, syms): self.calls.append(("l1_sub", syms))


def test_switch_symbol_sequence_and_guard():
    fs = FakeStream()
    state.set_active_symbol("GOOG")
    new = asyncio.run(feeds._switch_symbol(fs, "GOOG", "AAPL", trades=True))
    assert new == "AAPL"
    assert state.get_active_symbol() == "AAPL"      # guard re-armed for new symbol
    assert fs.calls == [
        ("book_unsub", ["GOOG"]), ("book_sub", ["AAPL"]),
        ("l1_unsub", ["GOOG"]), ("l1_sub", ["AAPL"]),
    ]


def test_switch_symbol_without_trades_skips_level_one():
    fs = FakeStream()
    asyncio.run(feeds._switch_symbol(fs, "GOOG", "AAPL", trades=False))
    assert all(not c[0].startswith("l1") for c in fs.calls)
    assert fs.calls == [("book_unsub", ["GOOG"]), ("book_sub", ["AAPL"])]


def test_stream_control_noop_until_bound():
    c = feeds.StreamControl()
    c.request_switch("AAPL")               # must not raise when unbound


def test_stream_control_posts_to_loop():
    c = feeds.StreamControl()

    async def driver():
        q = asyncio.Queue()
        c._bind(asyncio.get_running_loop(), q)
        c.request_switch("AAPL")           # uses call_soon_threadsafe
        await asyncio.sleep(0)             # let the scheduled callback run
        return await q.get()

    assert asyncio.run(driver()) == ("switch", "AAPL")


def test_heatmap_reset_clears_view():
    hm = Heatmap(n_cols=10, n_rows=120, tick=0.01, autofit=False)
    hm.push(_book("GOOG"), [])
    assert hm.row0_price is not None
    hm.reset()
    assert hm.row0_price is None and hm.last_book is None
    assert hm.cvd == 0.0 and len(hm.trades) == 0
    assert hm.matrix.sum() == 0
