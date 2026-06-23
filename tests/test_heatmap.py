"""Tests for the rolling heatmap: recentering, auto-fit, trades, CVD."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orderbook import Book, Level, Trade  # noqa: E402
from schwab_orderflow_heatmap import Heatmap  # noqa: E402


def book_at(mid, tick=0.01, levels=10, vol=100):
    bids = tuple(Level(round(mid - i * tick, 4), vol) for i in range(1, levels + 1))
    asks = tuple(Level(round(mid + i * tick, 4), vol) for i in range(1, levels + 1))
    return Book(symbol="SIM", ts_ms=0, bids=bids, asks=asks)


def test_autofit_centers_on_first_mid():
    hm = Heatmap(n_cols=50, tick=0.01, autofit=True)
    hm.push(book_at(100.0), [])
    rp = hm.row_prices()
    assert rp[0] < 100.0 < rp[-1]               # mid inside the window
    mid_row = (100.0 - hm.row0_price) / hm.tick
    assert abs(mid_row - hm.n_rows / 2) < 2     # roughly centered


def test_window_follows_drifting_price_without_clipping():
    hm = Heatmap(n_cols=50, n_rows=120, tick=0.01, autofit=False)
    hm.push(book_at(100.0), [])
    # drift far above the original window
    for m in [100.0 + i * 0.01 for i in range(1, 200)]:
        hm.push(book_at(m), [])
    rp = hm.row_prices()
    assert rp[0] < 101.99 < rp[-1]              # new mid still visible (recentered)
    # the latest column has nonzero liquidity (book not clipped away)
    assert hm.matrix[:, -1].sum() > 0


def test_deadzone_avoids_constant_reshift():
    hm = Heatmap(n_cols=20, n_rows=120, tick=0.01, autofit=False)
    hm.push(book_at(100.0), [])
    base = hm.row0_price
    hm.push(book_at(100.02), [])                # tiny move within dead-zone
    assert hm.row0_price == base               # no recenter


def test_trades_buffer_ages_and_drops():
    hm = Heatmap(n_cols=5, n_rows=120, tick=0.01, autofit=False)
    hm.push(book_at(100.0), [])
    hm.push(book_at(100.0), [Trade("SIM", 0, 100.0, 500, 1)])
    assert len(hm.trades) == 1 and hm.trades[-1]["col"] == 4
    for _ in range(5):                          # scroll it off the left edge
        hm.push(book_at(100.0), [])
    assert len(hm.trades) == 0


def test_cvd_integrates_signed_size():
    hm = Heatmap(n_cols=10, n_rows=120, tick=0.01, autofit=False)
    hm.push(book_at(100.0), [])
    hm.push(book_at(100.0), [Trade("SIM", 0, 100.05, 300, 1)])   # +300
    hm.push(book_at(100.0), [Trade("SIM", 0, 99.95, 100, -1)])   # -100
    assert hm.cvd == 200
    assert hm.cvd_hist[-1] == 200


def test_autofit_sets_view_and_wide_store():
    # autofit picks a sensible VIEW band (~0.15% of price) but the matrix STORE is wider,
    # so zooming out later reveals already-binned history instead of empty rows.
    hm = Heatmap(n_cols=20, tick=0.01, autofit=True)
    hm.push(book_at(300.0, levels=20), [])
    assert 60 <= hm.view_rows <= 240
    assert hm.matrix.shape == (hm.n_rows, hm.n_cols)
    assert hm.n_rows >= 4 * hm.view_rows               # store comfortably wider than view


def test_set_zoom_changes_view_only_and_preserves_data():
    hm = Heatmap(n_cols=20, tick=0.01, autofit=True)
    hm.push(book_at(100.0, levels=20), [])
    store0, before = hm.n_rows, hm.matrix.sum()
    center0 = hm.row0_price + (hm.n_rows // 2) * hm.tick
    hm.set_zoom(int(hm.view_rows * 1.5))               # zoom out
    assert hm.view_rows != 0
    assert hm.matrix.sum() == before                   # liquidity NOT rebuilt/lost
    assert hm.n_rows == store0                          # store untouched (fits the view)
    center1 = hm.row0_price + (hm.n_rows // 2) * hm.tick
    assert abs(center1 - center0) < hm.tick * 1.5       # store still anchored
    assert hm.autofit is False                          # manual zoom pins it


def test_set_zoom_clamped():
    hm = Heatmap(n_cols=20, tick=0.01, autofit=False, n_rows=120)
    hm.push(book_at(100.0), [])
    hm.set_zoom(5)
    assert hm.view_rows == 30                            # min clamp (view, not store)
    hm.set_zoom(9999)
    assert hm.view_rows == 600 and hm.matrix.shape[0] >= 600   # store grew to fit


def test_imbalance_sign():
    hm = Heatmap(n_cols=5, autofit=False, n_rows=120)
    heavy_bid = Book("SIM", 0,
                     bids=(Level(99.99, 1000), Level(99.98, 1000)),
                     asks=(Level(100.01, 100),))
    hm.push(heavy_bid, [])
    assert hm.imbalance() > 0
