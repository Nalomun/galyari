"""Liquidity-vacuum / runway score (GALYARI_EDGE_PLAN.md Phase C1).

The single most defensible breakout read in the whole category, and computable directly from
the snapshot book: how clear is the path *beyond* a level? For a long breakout, measure the
displayed ask liquidity in the N ticks above resistance; thin = vacuum = the breakout can run,
thick = it grinds. Mirror for shorts (bids below support).

A pure consumer of the parsed book — stdlib only. The key lesson from Phase B's real-tape run
(`VALIDATION.md` regime table): **"thin" is relative.** A 400-share lane is a vacuum on GOOG
and a wall on a $1.40 micro-cap, so the lane is normalized against the *name's own* typical
level size, not an absolute. That makes the score regime-portable.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class RunwayScore:
    price: float
    side: str            # 'resistance' (look up at asks) | 'support' (look down at bids)
    n_ticks: int
    beyond_size: float   # total displayed size in the lane beyond the level
    typical: float       # the name's typical (median) level size — the normalizer
    fill_ratio: float    # beyond_size / (n_ticks · typical); 0 empty, 1 every tick typical
    runway: float        # clamp(1 − fill_ratio, 0, 1); 1 = clear lane, 0 = fully walled
    blocker_ratio: float # largest single level in the lane / typical (a wall in the path)
    occupancy: float     # fraction of the N ticks that carry any size


def typical_level_size(book) -> float:
    """Median nonzero displayed level size across the current book — the per-name normalizer."""
    sizes = sorted(v for _, v in book.levels() if v > 0)
    if not sizes:
        return 0.0
    return float(statistics.median(sizes))


def _lane(book, price, side, n_ticks, tick):
    """The displayed levels strictly beyond the level, in breakout direction."""
    span = n_ticks * tick
    if side == "resistance":
        return [l.volume for l in book.asks
                if price + tick / 2 <= l.price <= price + span + 1e-9]
    if side == "support":
        return [l.volume for l in book.bids
                if price - span - 1e-9 <= l.price <= price - tick / 2]
    return []


def infer_side(price, mid, band=0.0) -> str:
    """Classify a level as resistance/support/at relative to the mid."""
    if mid is None:
        return "at"
    if price > mid + band:
        return "resistance"
    if price < mid - band:
        return "support"
    return "at"


def runway_score(book, price, side="auto", n_ticks: int = 10, tick: float = 0.01,
                 typical: float | None = None) -> RunwayScore | None:
    """Runway beyond `price` in the breakout direction. `side='auto'` infers from the mid.

    Returns None if the side can't be determined (level sits at the mid) or the book is empty.
    """
    if book is None or not (book.bids or book.asks):
        return None
    if side == "auto":
        side = infer_side(price, book.mid)
    if side not in ("resistance", "support"):
        return None
    typ = typical if typical is not None else typical_level_size(book)
    if typ <= 0:
        return None

    lane = _lane(book, price, side, n_ticks, tick)
    beyond = float(sum(lane))
    expected = n_ticks * typ
    fill = beyond / expected if expected > 0 else 0.0
    runway = max(0.0, min(1.0, 1.0 - fill))
    blocker = (max(lane) / typ) if lane else 0.0
    occupancy = len([v for v in lane if v > 0]) / n_ticks if n_ticks else 0.0
    return RunwayScore(price=round(price, 6), side=side, n_ticks=n_ticks,
                       beyond_size=beyond, typical=typ, fill_ratio=round(fill, 4),
                       runway=round(runway, 4), blocker_ratio=round(blocker, 4),
                       occupancy=round(occupancy, 4))


def runway_label(rs: RunwayScore, wall_mult: float = 4.0) -> str:
    """One-glance read: 'clear' (run), 'blocked' (grind/wall), or 'mixed'."""
    if rs is None:
        return "n/a"
    if rs.blocker_ratio >= wall_mult or rs.runway < 0.34:
        return "blocked"
    if rs.runway >= 0.66 and rs.blocker_ratio < 2.0:
        return "clear"
    return "mixed"
