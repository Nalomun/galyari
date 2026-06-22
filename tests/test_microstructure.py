"""Wall / pull / iceberg detection, driven by deterministic book+trade sequences."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orderbook import Book, Level, Trade  # noqa: E402
from microstructure import MicrostructureAnalyzer  # noqa: E402

TICK = 0.01


def mk_book(mid, specials=None, base=500, n=8):
    """Book with `base` size at every level except prices in `specials` (price->size)."""
    specials = specials or {}
    bids, asks = [], []
    for i in range(1, n + 1):
        bp = round(mid - i * TICK, 4)
        ap = round(mid + i * TICK, 4)
        bids.append(Level(bp, specials.get(bp, base)))
        asks.append(Level(ap, specials.get(ap, base)))
    return Book("T", 0, bids=tuple(bids), asks=tuple(asks))


def has(items, price, **attrs):
    for it in items:
        if abs(it.price - price) < 1e-9 and all(getattr(it, k) == v for k, v in attrs.items()):
            return True
    return False


def test_persistent_big_level_is_a_wall():
    a = MicrostructureAnalyzer(tick=TICK, wall_min_big_frames=10)
    ms = None
    for _ in range(12):
        ms = a.update(mk_book(100.0, {99.97: 8000}))
    assert has(ms.walls, 99.97, side="bid")
    w = next(w for w in ms.walls if abs(w.price - 99.97) < 1e-9)
    assert w.big_frames >= 10 and w.persistence > 0


def test_transient_spike_is_not_a_wall():
    a = MicrostructureAnalyzer(tick=TICK, wall_min_big_frames=10)
    ms = a.update(mk_book(100.0, {99.97: 8000}))   # single frame only
    for _ in range(3):
        ms = a.update(mk_book(100.0))              # back to normal
    assert not ms.walls


def test_pull_fires_when_wall_disappears():
    a = MicrostructureAnalyzer(tick=TICK, wall_min_big_frames=10)
    for _ in range(12):
        a.update(mk_book(100.0, {99.97: 8000}))    # build the wall
    ms = a.update(mk_book(100.0))                  # wall yanked
    assert has(ms.pulls, 99.97, side="bid")
    assert not has(ms.walls, 99.97)                # no longer reported as a wall


def test_iceberg_refills_under_execution():
    a = MicrostructureAnalyzer(tick=TICK)
    price = 99.95
    ms = None
    for s in (300, 150, 400, 200, 450):            # deplete/recover twice
        ms = a.update(mk_book(100.0, {price: s}),
                      [Trade("T", 0, price, 300, -1)])   # heavy execution at price
    assert has(ms.icebergs, price)
    ic = next(ic for ic in ms.icebergs if abs(ic.price - price) < 1e-9)
    assert ic.refills >= 2 and ic.executed >= ic.displayed * 3


def test_no_iceberg_without_execution():
    a = MicrostructureAnalyzer(tick=TICK)
    price = 99.95
    ms = None
    for s in (300, 150, 400, 200, 450):            # refills but no trades
        ms = a.update(mk_book(100.0, {price: s}))
    assert not ms.icebergs


def test_empty_book_is_safe():
    a = MicrostructureAnalyzer(tick=TICK)
    ms = a.update(None)
    assert ms.walls == () and ms.pulls == () and ms.icebergs == ()
    ms = a.update(Book("T", 0, bids=(), asks=()))
    assert ms.walls == ()


def test_reset_clears_state():
    a = MicrostructureAnalyzer(tick=TICK, wall_min_big_frames=10)
    for _ in range(12):
        a.update(mk_book(100.0, {99.97: 8000}))
    assert a.levels and a.active_walls
    a.reset()
    assert a.t == 0 and not a.levels and not a.active_walls and a.last_mid is None


def test_window_pruning_bounds_state():
    a = MicrostructureAnalyzer(tick=TICK, window=20, wall_min_big_frames=5)
    for _ in range(200):                           # long run shouldn't grow unbounded
        a.update(mk_book(100.0, {99.97: 8000}))
    # only levels near the (static) book remain; ~16 prices + the wall, well under 100
    assert len(a.levels) < 100
