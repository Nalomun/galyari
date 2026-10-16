"""Tests for the runway / liquidity-vacuum score (runway.py, Phase C1)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orderbook import Book, Level
from runway import runway_score, runway_label, typical_level_size, infer_side


def mk_book(bids, asks):
    return Book("T", 0,
                tuple(Level(round(p, 4), v) for p, v in bids),
                tuple(Level(round(p, 4), v) for p, v in asks))


def test_clear_lane_scores_high():
    # mid ~100.005, resistance at 100.05 with nothing above it → clear runway
    bids = [(round(100.00 - i * 0.01, 2), 1000) for i in range(6)]
    asks = [(round(100.01 + i * 0.01, 2), 1000) for i in range(4)]   # stops at 100.04
    rs = runway_score(mk_book(bids, asks), 100.05, "resistance", n_ticks=10)
    assert rs.runway == 1.0 and rs.beyond_size == 0
    assert runway_label(rs) == "clear"


def test_walled_lane_scores_low():
    # a thick, evenly-filled lane above the level → no runway
    bids = [(round(100.00 - i * 0.01, 2), 1000) for i in range(6)]
    asks = [(round(100.01 + i * 0.01, 2), 1000) for i in range(20)]  # full lane at typical
    rs = runway_score(mk_book(bids, asks), 100.05, "resistance", n_ticks=10)
    assert rs.runway < 0.2
    assert runway_label(rs) == "blocked"


def test_single_blocker_flags_blocked():
    # lane mostly empty but one giant wall sitting in it → blocked despite low fill
    bids = [(100.00, 1000)]
    asks = [(100.01, 1000), (100.08, 20000)]   # 20k wall at 100.08, typical ~1000
    rs = runway_score(mk_book(bids, asks), 100.05, "resistance", n_ticks=10)
    assert rs.blocker_ratio >= 4.0
    assert runway_label(rs) == "blocked"


def test_support_side_mirrors():
    # breakdown: thin bids below the support level → clear runway down
    bids = [(99.99, 1000)]                      # nothing in (99.89, 99.94]
    asks = [(100.00, 1000)]
    rs = runway_score(mk_book(bids, asks), 99.95, "support", n_ticks=5)
    assert rs.side == "support" and rs.runway == 1.0


def test_normalized_to_name_depth():
    # the SAME absolute lane size reads differently against different typical depths
    thin_name = mk_book([(1.40, 100)], [(1.41, 100), (1.45, 400)])
    thick_name = mk_book([(50.00, 5000)], [(50.01, 5000), (50.05, 400)])
    rs_thin = runway_score(thin_name, 1.42, "resistance", n_ticks=10)
    rs_thick = runway_score(thick_name, 50.02, "resistance", n_ticks=10)
    # 400 shares is a meaningful blocker on the $1.40 name, trivial on the $50 name
    assert rs_thin.fill_ratio > rs_thick.fill_ratio


def test_auto_side_and_at_mid_returns_none():
    book = mk_book([(100.00, 1000)], [(100.01, 1000)])
    assert runway_score(book, 105.00, "auto").side == "resistance"
    assert runway_score(book, 95.00, "auto").side == "support"
    # a level sitting on the mid has no breakout direction
    assert runway_score(book, 100.005, "auto") is None


def test_typical_and_infer_helpers():
    book = mk_book([(100.00, 1000), (99.99, 3000)], [(100.01, 1000)])
    assert typical_level_size(book) == 1000
    assert infer_side(101, 100.0) == "resistance"
    assert infer_side(99, 100.0) == "support"
    assert infer_side(100, None) == "at"
