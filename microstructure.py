"""Liquidity-wall / pull / iceberg detection over the parsed book + trade stream.

A pure *consumer* of the stable core (`orderbook.Book`/`Trade`) — stdlib only, no
matplotlib, no numpy. Feed it one `(book, trades)` per render tick via `update()`; it
returns the currently-detected features. State is a sliding window of `window` ticks.

Definitions (see DATA_SCHEMA / PHASE5.md 5C):
  - Wall    : a price level that is *currently* large relative to the book AND has been
              large for at least `wall_min_big_frames` frames in the window (persistence).
  - Pull    : a level that was a wall last tick and whose size just collapsed (< pull_drop
              of its wall size) — often precedes a fast move.
  - Iceberg : a level that keeps *executing* (trades printing there) yet *refills* its
              displayed size — cumulative traded volume >> displayed size, with repeated
              deplete→recover cycles.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Wall:
    price: float
    side: str           # 'bid' | 'ask'
    size: float         # current displayed size
    persistence: float  # big_frames / window, in [0, 1]
    big_frames: int


@dataclass(frozen=True)
class Pull:
    price: float
    side: str
    prev_size: float    # the wall size just before it was pulled


@dataclass(frozen=True)
class Iceberg:
    price: float
    side: str
    executed: float     # cumulative traded volume at price in window
    displayed: float    # typical displayed size
    refills: int


@dataclass(frozen=True)
class MicroState:
    walls: tuple[Wall, ...]
    pulls: tuple[Pull, ...]
    icebergs: tuple[Iceberg, ...]


class _Level:
    __slots__ = ("side", "hist", "execs", "refills", "last_size", "depleted",
                 "last_exec_t")

    def __init__(self, side: str):
        self.side = side
        self.hist: deque = deque()      # (t, size, is_big)
        self.execs: deque = deque()     # (t, volume)
        self.refills: deque = deque()   # (t,) — recoveries that followed execution
        self.last_size = 0.0
        self.depleted = False
        self.last_exec_t = -10 ** 9     # frame of the most recent trade here

    def empty(self) -> bool:
        return not self.hist and not self.execs


class MicrostructureAnalyzer:
    def __init__(self, tick: float = 0.01, window: int = 120,
                 wall_mult: float = 4.0, wall_min_big_frames: int = 10,
                 pull_drop: float = 0.5,
                 iceberg_exec_mult: float = 4.0, iceberg_min_refills: int = 2,
                 iceberg_min_exec: float = 1500.0,
                 refill_drop: float = 0.6, refill_recover: float = 1.3,
                 refill_exec_window: int = 6):
        self.tick = tick
        self.window = window
        self.wall_mult = wall_mult
        self.wall_min_big_frames = wall_min_big_frames
        self.pull_drop = pull_drop
        self.iceberg_exec_mult = iceberg_exec_mult
        self.iceberg_min_refills = iceberg_min_refills
        self.iceberg_min_exec = iceberg_min_exec
        self.refill_drop = refill_drop
        self.refill_recover = refill_recover
        self.refill_exec_window = refill_exec_window

        self.t = 0
        self.levels: dict[int, _Level] = {}
        self.active_walls: dict[int, float] = {}   # key -> wall size last tick
        self.last_mid: float | None = None

    def reset(self) -> None:
        """Drop all accumulated state — used on a live symbol switch."""
        self.t = 0
        self.levels.clear()
        self.active_walls.clear()
        self.last_mid = None

    def _key(self, price: float) -> int:
        return int(round(price / self.tick))

    def _side_of(self, price: float) -> str:
        if self.last_mid is None:
            return "bid"
        return "bid" if price <= self.last_mid else "ask"

    def update(self, book, trades=()) -> MicroState:
        self.t += 1
        t, W = self.t, self.window

        if book is not None and (book.bids or book.asks):
            if book.mid is not None:
                self.last_mid = book.mid
            sizes = [v for _, v in book.levels()]
            med = statistics.median(sizes) if sizes else 0.0
            big_thr = self.wall_mult * med if med > 0 else float("inf")

            for side, levels in (("bid", book.bids), ("ask", book.asks)):
                for lvl in levels:
                    L = self.levels.get(self._key(lvl.price))
                    if L is None:
                        L = _Level(side)
                        self.levels[self._key(lvl.price)] = L
                    L.side = side
                    # deplete -> recover bookkeeping. A recovery only counts as a
                    # refill if it followed *execution* at this price recently — that
                    # is what distinguishes a reloading iceberg from size noise.
                    if L.last_size > 0 and lvl.volume <= L.last_size * self.refill_drop:
                        L.depleted = True
                    elif L.depleted and lvl.volume >= L.last_size * self.refill_recover:
                        if t - L.last_exec_t <= self.refill_exec_window:
                            L.refills.append(t)
                        L.depleted = False
                    L.last_size = lvl.volume
                    L.hist.append((t, lvl.volume, lvl.volume >= big_thr))

            for tr in trades:
                k = self._key(tr.price)
                L = self.levels.get(k)
                if L is None:
                    L = _Level(self._side_of(tr.price))
                    self.levels[k] = L
                L.execs.append((t, tr.size))
                L.last_exec_t = t

        self._prune(t - W)

        walls, icebergs = [], []
        new_active: dict[int, float] = {}
        for k, L in self.levels.items():
            big_frames = sum(1 for (_, _, b) in L.hist if b)
            present_now = bool(L.hist) and L.hist[-1][0] == t
            size_now = L.hist[-1][1] if present_now else 0.0
            big_now = present_now and L.hist[-1][2]

            if big_now and big_frames >= self.wall_min_big_frames:
                walls.append(Wall(k * self.tick, L.side, size_now,
                                  big_frames / W, big_frames))
                new_active[k] = size_now

            executed = sum(v for (_, v) in L.execs)
            disp = [s for (_, s, _) in L.hist if s > 0]
            disp_med = statistics.median(disp) if disp else 0.0
            refills = len(L.refills)
            if (executed >= self.iceberg_min_exec and disp_med > 0
                    and executed >= self.iceberg_exec_mult * disp_med
                    and refills >= self.iceberg_min_refills):
                icebergs.append(Iceberg(k * self.tick, L.side, executed,
                                        disp_med, refills))

        pulls = []
        for k, prev_size in self.active_walls.items():
            if k in new_active:
                continue
            L = self.levels.get(k)
            cur = L.hist[-1][1] if (L and L.hist and L.hist[-1][0] == t) else 0.0
            if cur <= prev_size * self.pull_drop:
                price = k * self.tick
                side = L.side if L else self._side_of(price)
                pulls.append(Pull(price, side, prev_size))

        self.active_walls = new_active
        self._gc()
        walls.sort(key=lambda w: -w.size)
        return MicroState(tuple(walls), tuple(pulls), tuple(icebergs))

    def _prune(self, cutoff: int) -> None:
        for L in self.levels.values():
            while L.hist and L.hist[0][0] <= cutoff:
                L.hist.popleft()
            while L.execs and L.execs[0][0] <= cutoff:
                L.execs.popleft()
            while L.refills and L.refills[0] <= cutoff:
                L.refills.popleft()

    def _gc(self) -> None:
        dead = [k for k, L in self.levels.items()
                if L.empty() and k not in self.active_walls]
        for k in dead:
            del self.levels[k]
