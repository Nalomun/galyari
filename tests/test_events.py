"""Tests for the event store + forward-outcome labeler (events.py, Phase E1/E2)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orderbook import Book, Level, Trade
from events import (Event, EventExtractor, book_imbalance, label_events,
                    extract_from_tape, build_summary, HORIZONS_MS)


def mk_book(bids, asks, ts=0, symbol="T"):
    return Book(symbol=symbol, ts_ms=ts,
                bids=tuple(Level(round(p, 4), v) for p, v in bids),
                asks=tuple(Level(round(p, 4), v) for p, v in asks))


def flat_book(mid_bid=100.00, n=8, size=1000, ts=0):
    bids = [(round(mid_bid - i * 0.01, 2), size) for i in range(n)]
    asks = [(round(mid_bid + 0.01 + i * 0.01, 2), size) for i in range(n)]
    return mk_book(bids, asks, ts=ts)


# --- E1 ------------------------------------------------------------------- #
def test_book_imbalance_matches_formula():
    bk = mk_book([(100.0, 300)], [(100.01, 100)])
    assert book_imbalance(bk) == (300 - 100) / 400
    assert book_imbalance(mk_book([], [])) is None


def test_extractor_emits_level_state_event_with_context():
    ex = EventExtractor(auto_levels=False)
    ex.levels.add_manual(100.05, "manual")
    bids = [(round(100.00 - i * 0.01, 2), 1000) for i in range(6)]
    asks = [(round(100.01 + i * 0.01, 2), 1000) for i in range(4)]  # vacuum beyond
    evs = ex.process(mk_book(bids, asks, ts=0), [], 0)
    le = [e for e in evs if e.kind == "level_state"]
    assert le and le[0].detail["new_state"] == "vacuum"
    e = le[0]
    assert e.mid is not None and e.imbalance is not None and e.ts_ms == 0
    assert e.side == "resistance"


def test_extractor_dedupes_iceberg_appearance():
    # craft a level that keeps executing and refilling so the iceberg detector fires,
    # then confirm only ONE iceberg event is emitted across repeated frames.
    ex = EventExtractor(auto_levels=False)
    bids = [(round(100.00 - i * 0.01, 2), 800) for i in range(6)]
    asks_full = [(round(100.01 + i * 0.01, 2), 800) for i in range(6)]
    iceberg_events = 0
    for i in range(40):
        # alternate deplete/refill at the best ask (100.01) under steady execution
        sz = 200 if i % 2 else 800
        asks = [(p, sz if abs(p - 100.01) < 1e-9 else v) for p, v in asks_full]
        trs = [Trade("T", i * 100, 100.01, 600, 1)]
        evs = ex.process(mk_book(bids, asks, ts=i * 100), trs, i * 100)
        iceberg_events += sum(1 for e in evs if e.kind == "iceberg")
    assert iceberg_events <= 1   # deduped to the appearance (0 or 1, never per-frame spam)


# --- E2 ------------------------------------------------------------------- #
def test_label_forward_returns_and_excursion():
    ev = Event(ts_ms=0, symbol="T", kind="pull", label="", side="ask", price=100.05,
               mid=100.00, spread=0.01, imbalance=0.0)
    # mid rises to 101.00 by 30s, dips to 99.50 along the way
    series = {"T": [(0, 100.00), (10_000, 99.50), (30_000, 101.00),
                    (120_000, 100.50), (300_000, 100.00)]}
    n = label_events([ev], series)
    assert n == 1
    assert ev.labels["ret_bps_30s"] == round((101.0 - 100.0) / 100.0 * 1e4, 2)  # +100 bps
    assert ev.labels["mfe_up_bps"] == round((101.0 - 100.0) / 100.0 * 1e4, 2)
    assert ev.labels["mae_dn_bps"] == round((99.5 - 100.0) / 100.0 * 1e4, 2)    # −50 bps


def test_label_none_when_no_forward_data():
    ev = Event(ts_ms=290_000, symbol="T", kind="pull", label="", side="ask",
               price=100.0, mid=100.0, spread=0.01, imbalance=0.0)
    series = {"T": [(290_000, 100.0)]}        # nothing far enough ahead
    label_events([ev], series)
    assert "ret_bps_30s" not in ev.labels      # no horizon could be labeled


# --- integration ---------------------------------------------------------- #
def test_extract_and_label_from_real_tape():
    import glob
    tapes = glob.glob(os.path.join(os.path.dirname(__file__), "..", "recordings", "*.jsonl"))
    if not tapes:
        pytest.skip("no recordings/*.jsonl tapes found (recordings are not distributed)")
    events, mids = extract_from_tape(sorted(tapes)[0])
    assert isinstance(events, list)
    label_events(events, mids)
    # at least the summary builds without error
    md = build_summary(events)
    assert "SIGNALS_PREVIEW" in md
