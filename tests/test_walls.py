"""Tests for the wall lifecycle classifier (walls.py, Phase C3)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orderbook import Book, Level, Trade
from walls import WallTracker, WallResolution
from microstructure import Wall, MicroState


def mk_book(bids, asks, ts=0):
    return Book("T", ts,
                tuple(Level(round(p, 4), v) for p, v in bids),
                tuple(Level(round(p, 4), v) for p, v in asks))


def micro_with_wall(price, side, size):
    """A MicroState that reports one wall — stands in for the analyzer's detection."""
    return MicroState(walls=(Wall(price, side, size, 1.0, 20),), pulls=(), icebergs=())


def book_with_ask_wall(wall_price, wall_size, ts=0):
    bids = [(99.99, 500), (99.98, 500)]
    asks = [(100.00, 500), (wall_price, wall_size), (100.05, 500)]
    return mk_book(bids, asks, ts=ts)


def test_pull_resolution():
    # a big ask wall that simply vanishes un-eaten → PULL
    wt = WallTracker(grace_ms=1000)
    p = 100.02
    wt.update(book_with_ask_wall(p, 10000, ts=0), [], 0, micro_with_wall(p, "ask", 10000))
    # wall gone (size ~0), no trades; let it stay collapsed past the grace window
    res = []
    for ts in (500, 1500, 2500):
        res += wt.update(book_with_ask_wall(p, 0, ts=ts), [], ts, None)
    assert len(res) == 1 and res[0].resolution == "pull"
    assert res[0].exec_total == 0


def test_eaten_resolution():
    # the wall is consumed by heavy execution before it disappears → EATEN
    wt = WallTracker(grace_ms=1000, eaten_frac=0.5)
    p = 100.02
    wt.update(book_with_ask_wall(p, 10000, ts=0), [], 0, micro_with_wall(p, "ask", 10000))
    # 8000 shares trade through the level, then it's gone
    trs = [Trade("T", 100, 100.02, 8000, 1)]
    wt.update(book_with_ask_wall(p, 0, ts=200), trs, 200, None)
    res = []
    for ts in (700, 1700):
        res += wt.update(book_with_ask_wall(p, 0, ts=ts), [], ts, None)
    assert len(res) == 1 and res[0].resolution == "eaten"
    assert res[0].exec_total == 8000


def test_refresh_resolution():
    # the wall depletes and reloads repeatedly under execution → REFRESH (absorption)
    wt = WallTracker(grace_ms=1000, refresh_min=2, refill_exec_window_ms=8000)
    p = 100.02
    m = micro_with_wall(p, "ask", 10000)
    seq = [(10000, []), (3000, [Trade("T", 0, 100.02, 4000, 1)]), (10000, []),  # refill 1
           (3000, [Trade("T", 0, 100.02, 4000, 1)]), (10000, [])]               # refill 2
    last = None
    for i, (sz, trs) in enumerate(seq):
        ts = i * 500
        trs = [Trade("T", ts, 100.02, 4000, 1)] if trs else []
        last = wt.update(book_with_ask_wall(p, sz, ts=ts), trs, ts, m)
    # now it finally collapses and stays gone → resolves as REFRESH
    res = []
    for ts in (4000, 5000, 6000):
        res += wt.update(book_with_ask_wall(p, 0, ts=ts), [], ts, None)
    assert len(res) == 1 and res[0].resolution == "refresh"
    assert res[0].refills >= 2


def test_no_resolution_while_present():
    # a wall that stays up the whole time never resolves
    wt = WallTracker(grace_ms=1000)
    p = 100.02
    m = micro_with_wall(p, "ask", 10000)
    res = []
    for ts in (0, 1000, 2000, 3000):
        res += wt.update(book_with_ask_wall(p, 10000, ts=ts), [], ts, m)
    assert res == []


def test_grace_prevents_premature_resolution():
    # a one-frame dip that recovers within grace must NOT resolve
    wt = WallTracker(grace_ms=3000)
    p = 100.02
    m = micro_with_wall(p, "ask", 10000)
    wt.update(book_with_ask_wall(p, 10000, ts=0), [], 0, m)
    r1 = wt.update(book_with_ask_wall(p, 0, ts=500), [], 500, None)     # dip
    r2 = wt.update(book_with_ask_wall(p, 10000, ts=1000), [], 1000, m)  # recovered
    assert r1 == () and r2 == ()
    assert wt._walls                                                     # still tracked
