"""Tests for the level-centric layer (levels.py, Phase B).

Each state in the machine (DEFENDED / ABSORBING / VACUUM / BROKEN) is driven by a
hand-built sequence of book snapshots + trades so the classification is exercised
deterministically, plus the manual/auto level plumbing and event emission.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orderbook import Book, Level, Trade
from levels import LevelTracker, LevelState, LevelOfInterest, LevelEvent


def mk_book(bid_levels, ask_levels, ts=0, symbol="T"):
    """bid_levels / ask_levels: list of (price, volume)."""
    bids = tuple(Level(round(p, 4), v) for p, v in bid_levels)
    asks = tuple(Level(round(p, 4), v) for p, v in ask_levels)
    return Book(symbol=symbol, ts_ms=ts, bids=bids, asks=asks)


def flat_book(mid_bid=100.00, n=8, size=1000, ts=0):
    """A symmetric, evenly-sized book around a ~mid (best bid `mid_bid`, ask one tick up)."""
    bids = [(round(mid_bid - i * 0.01, 2), size) for i in range(n)]
    asks = [(round(mid_bid + 0.01 + i * 0.01, 2), size) for i in range(n)]
    return mk_book(bids, asks, ts=ts)


# --------------------------------------------------------------------------- #
# manual + auto level plumbing
# --------------------------------------------------------------------------- #
def test_add_and_remove_manual():
    lt = LevelTracker(auto=False)
    loi = lt.add_manual(100.057, "manual")          # snaps to the tick grid
    assert loi.price == 100.06 and loi.source == "manual"
    st, _ = lt.update(flat_book(), [], ts_ms=0)
    assert any(s.loi.label == "manual" for s in st)
    lt.remove_manual(100.06)
    st, _ = lt.update(flat_book(ts=1000), [], ts_ms=1000)
    assert not any(s.loi.label == "manual" for s in st)


def test_auto_levels_hod_lod_vwap():
    lt = LevelTracker(auto=True)
    book = flat_book()
    trades = [Trade("T", 0, 100.50, 100, 1), Trade("T", 0, 99.50, 100, -1)]
    st, _ = lt.update(book, trades, ts_ms=0)
    assert lt.hod == 100.50 and lt.lod == 99.50
    assert lt.vwap == 100.00                          # equal sizes either side
    labels = {s.loi.label for s in st}
    assert {"HOD", "LOD", "VWAP", "round"} <= labels


def test_auto_round_increment_scales_with_price():
    lt = LevelTracker(auto=True)
    # a sub-$5 micro-cap should use a $0.10 round grid, not whole dollars
    book = flat_book(mid_bid=1.40, n=6, size=500)
    st, _ = lt.update(book, [], ts_ms=0)
    rounds = sorted({s.loi.price for s in st if s.loi.label == "round"})
    assert 1.40 in rounds and 1.50 in rounds and 1.30 in rounds


# --------------------------------------------------------------------------- #
# state machine
# --------------------------------------------------------------------------- #
def test_vacuum_thin_book_beyond_resistance():
    lt = LevelTracker(auto=False)
    lt.add_manual(100.05, "manual")
    # book is thick up to 100.04, then nothing at/above the level → vacuum beyond
    bids = [(round(100.00 - i * 0.01, 2), 1000) for i in range(6)]
    asks = [(round(100.01 + i * 0.01, 2), 1000) for i in range(4)]  # 100.01..100.04
    st, ev = lt.update(mk_book(bids, asks), [], ts_ms=0)
    s = next(s for s in st if s.loi.label == "manual")
    assert s.state == LevelState.VACUUM and s.side == "resistance"
    assert s.beyond_thickness == 0
    assert any(e.new_state == "vacuum" for e in ev)


def test_defended_refreshing_under_execution():
    lt = LevelTracker(auto=False, refresh_min=2)
    lt.add_manual(100.05, "manual", band=0.0)   # exact price, so the deplete/refill is clean
    # thick book everywhere so 'beyond' is never thin (rule out VACUUM)
    def book(level_size, ts):
        bids = [(round(100.00 - i * 0.01, 2), 1000) for i in range(6)]
        asks = [(round(100.01 + i * 0.01, 2), 1000) for i in range(10)]
        # override the level's own size
        asks = [(p, level_size if abs(p - 100.05) < 1e-9 else v) for p, v in asks]
        return mk_book(bids, asks, ts=ts)

    trade = [Trade("T", 0, 100.05, 300, 1)]
    seq = [(1000, []), (500, trade), (1000, []),       # refill #1
           (500, trade), (1000, [])]                    # refill #2
    last = None
    for i, (sz, trs) in enumerate(seq):
        st, ev = lt.update(book(sz, ts=i * 1000),
                           [Trade("T", i * 1000, 100.05, 300, 1)] if trs else [],
                           ts_ms=i * 1000)
        last = next(s for s in st if s.loi.label == "manual")
    assert last.refresh_count >= 2
    assert last.state == LevelState.DEFENDED


def test_absorbing_heavy_volume_little_progress():
    lt = LevelTracker(auto=False, absorb_mult=6.0, refresh_min=2)
    lt.add_manual(100.05, "manual")
    bids = [(round(100.00 - i * 0.01, 2), 1000) for i in range(6)]
    asks = [(round(100.01 + i * 0.01, 2), 1000) for i in range(10)]  # thick beyond
    book = mk_book(bids, asks)
    last = None
    for i in range(4):
        # heavy selling into the level, size stays up (no deplete/refill), price flat
        trs = [Trade("T", i * 500, 100.05, 2000, -1)]
        st, ev = lt.update(mk_book(bids, asks, ts=i * 500), trs, ts_ms=i * 500)
        last = next(s for s in st if s.loi.label == "manual")
    assert last.refresh_count < 2
    assert last.volume_through >= 6 * 1000
    assert last.state == LevelState.ABSORBING


def test_broken_requires_cross_and_hold():
    lt = LevelTracker(auto=False, broken_ticks=1.0, broken_hold_ms=1500)
    lt.add_manual(100.05, "manual")
    # frame 1: mid below the level (100.005)
    st, _ = lt.update(flat_book(mid_bid=100.00, ts=0), [], ts_ms=0)
    s = next(s for s in st if s.loi.label == "manual")
    assert s.state != LevelState.BROKEN
    # frame 2: mid jumps above level+margin, but not held long enough yet
    st, _ = lt.update(flat_book(mid_bid=100.10, ts=2000), [], ts_ms=2000)
    s = next(s for s in st if s.loi.label == "manual")
    assert s.state != LevelState.BROKEN
    # frame 3: still above, now held > 1500ms since the cross → BROKEN
    st, ev = lt.update(flat_book(mid_bid=100.10, ts=4000), [], ts_ms=4000)
    s = next(s for s in st if s.loi.label == "manual")
    assert s.state == LevelState.BROKEN
    assert any(e.new_state == "broken" for e in ev)


def test_no_spurious_break_without_cross():
    # a level always above the mid (never crossed) must not be reported BROKEN
    lt = LevelTracker(auto=False)
    lt.add_manual(105.00, "manual")
    for i in range(4):
        st, _ = lt.update(flat_book(mid_bid=100.00, ts=i * 2000), [], ts_ms=i * 2000)
    s = next(s for s in st if s.loi.label == "manual")
    assert s.state != LevelState.BROKEN


# --------------------------------------------------------------------------- #
# events + lifecycle
# --------------------------------------------------------------------------- #
def test_events_only_on_transition():
    lt = LevelTracker(auto=False)
    lt.add_manual(100.05, "manual")
    bids = [(round(100.00 - i * 0.01, 2), 1000) for i in range(6)]
    asks = [(round(100.01 + i * 0.01, 2), 1000) for i in range(4)]  # vacuum beyond
    _, ev1 = lt.update(mk_book(bids, asks, ts=0), [], ts_ms=0)
    _, ev2 = lt.update(mk_book(bids, asks, ts=1000), [], ts_ms=1000)  # same state
    assert any(e.new_state == "vacuum" for e in ev1)
    assert all(e.label != "manual" for e in ev2)      # no repeat event while unchanged


def test_reset_clears_state():
    lt = LevelTracker(auto=True)
    lt.add_manual(100.05, "manual")
    lt.update(flat_book(), [Trade("T", 0, 100.5, 100, 1)], ts_ms=0)
    assert lt.hod is not None
    lt.reset()
    assert lt.hod is None and lt.vwap is None
    st, _ = lt.update(flat_book(ts=1000), [], ts_ms=1000)
    assert not any(s.loi.label == "manual" for s in st)   # manual levels cleared too


def test_window_prunes_old_volume():
    lt = LevelTracker(auto=False, window_ms=5000)
    lt.add_manual(100.05, "manual")
    bids = [(round(100.00 - i * 0.01, 2), 1000) for i in range(6)]
    asks = [(round(100.01 + i * 0.01, 2), 1000) for i in range(10)]
    lt.update(mk_book(bids, asks, ts=0),
              [Trade("T", 0, 100.05, 5000, -1)], ts_ms=0)
    # 10s later (> window), the old volume should have aged out
    st, _ = lt.update(mk_book(bids, asks, ts=10000), [], ts_ms=10000)
    s = next(s for s in st if s.loi.label == "manual")
    assert s.volume_through == 0
