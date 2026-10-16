"""Wall lifecycle classifier (GALYARI_EDGE_PLAN.md Phase C3).

`microstructure.py` detects walls, pulls and icebergs *separately*. This unifies them into a
single per-wall lifecycle that ends in exactly one of three labels — which mean opposite
things and are the natural unit for Phase E measurement:

  REFRESH — replenished repeatedly when hit → real interest / absorption → fade/grind read.
  PULL    — vanished un-eaten → makers stepped back → often precedes the run.
  EATEN   — executed through → real supply consumed → momentum through.

A pure consumer (stdlib only). Feed `update(book, trades, ts_ms, micro)` per tick, passing the
`MicroState` from the analyzer so this module reuses the existing wall *detection* and only
adds the lifecycle tracking + terminal classification. Returns the resolutions that fired this
tick.

Robustness notes grounded in Phase A (`VALIDATION.md`):
  - Everything is wall-clock-time–windowed (median Δt ~1–1.5s; frame counts drift).
  - A wall isn't resolved the instant its size dips — a REFRESH dips and recovers fast. We
    require the size to stay collapsed for `grace_ms` before resolving, so a reload is not
    mistaken for a pull.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class WallResolution:
    ts_ms: int
    symbol: str
    price: float
    side: str                # 'bid' | 'ask'
    resolution: str          # 'refresh' | 'pull' | 'eaten'
    peak_size: float
    exec_total: float        # gross volume traded at the level over its life
    refills: int
    lifetime_ms: int


class _Wall:
    __slots__ = ("side", "born_ts", "peak", "last_size", "exec_total", "refills",
                 "depleted", "last_exec_ts", "collapsed_since", "exec_hist")

    def __init__(self, side, ts, size):
        self.side = side
        self.born_ts = ts
        self.peak = size
        self.last_size = size
        self.exec_total = 0.0
        self.refills = 0
        self.depleted = False
        self.last_exec_ts = -10 ** 18
        self.collapsed_since: int | None = None
        self.exec_hist: deque = deque()     # (ts, gross) — for the refill-recency check


class WallTracker:
    """Tracks each detected wall to its terminal label. One per symbol; `reset()` on switch."""

    def __init__(self, tick: float = 0.01, resolve_frac: float = 0.25,
                 grace_ms: int = 3_000, refresh_min: int = 2, eaten_frac: float = 0.5,
                 refill_drop: float = 0.6, refill_recover: float = 1.3,
                 refill_exec_window_ms: int = 8_000):
        self.tick = tick
        self.resolve_frac = resolve_frac
        self.grace_ms = grace_ms
        self.refresh_min = refresh_min
        self.eaten_frac = eaten_frac
        self.refill_drop = refill_drop
        self.refill_recover = refill_recover
        self.refill_exec_window_ms = refill_exec_window_ms

        self.symbol = ""
        self._walls: dict[int, _Wall] = {}

    def reset(self) -> None:
        self.symbol = ""
        self._walls.clear()

    def _key(self, price: float) -> int:
        return int(round(price / self.tick))

    def _size_at(self, book, key: int) -> float:
        """Displayed size at the wall's exact price level."""
        target = key * self.tick
        return sum(l.volume for l in (*book.bids, *book.asks)
                   if abs(l.price - target) <= self.tick / 2)

    def update(self, book, trades=(), ts_ms: int | None = None, micro=None):
        if book is None or not (book.bids or book.asks):
            return ()
        if book.symbol:
            self.symbol = book.symbol
        t = ts_ms if ts_ms is not None else book.ts_ms

        wall_keys = {self._key(w.price) for w in getattr(micro, "walls", ())} if micro else set()

        # birth: a newly detected wall starts a lifecycle
        for w in (getattr(micro, "walls", ()) if micro else ()):
            k = self._key(w.price)
            if k not in self._walls:
                self._walls[k] = _Wall(w.side, t, self._size_at(book, k))

        # execution at each tracked level this tick
        for tr in trades:
            k = self._key(tr.price)
            W = self._walls.get(k)
            if W is not None:
                W.exec_total += tr.size
                W.last_exec_ts = t
                W.exec_hist.append((t, tr.size))

        resolutions = []
        for k, W in list(self._walls.items()):
            size = self._size_at(book, k)
            W.peak = max(W.peak, size)
            if k in wall_keys:
                W.peak = max(W.peak, size)

            # deplete→recover refill bookkeeping (a reload that followed recent execution)
            if W.last_size > 0 and size <= W.last_size * self.refill_drop:
                W.depleted = True
            elif W.depleted and size >= W.last_size * self.refill_recover:
                if t - W.last_exec_ts <= self.refill_exec_window_ms:
                    W.refills += 1
                W.depleted = False
            W.last_size = size

            # resolution: size has stayed collapsed (below resolve_frac of peak) for grace_ms
            collapsed = W.peak > 0 and size <= self.resolve_frac * W.peak
            if collapsed:
                if W.collapsed_since is None:
                    W.collapsed_since = t
                elif t - W.collapsed_since >= self.grace_ms:
                    resolutions.append(self._resolve(k, W, t))
                    del self._walls[k]
            else:
                W.collapsed_since = None

        return tuple(resolutions)

    def _resolve(self, key, W, t) -> WallResolution:
        # priority: a wall that reloaded repeatedly is REFRESH even if also partly eaten;
        # otherwise heavy execution relative to its size is EATEN; else it was PULLed.
        if W.refills >= self.refresh_min:
            label = "refresh"
        elif W.peak > 0 and W.exec_total >= self.eaten_frac * W.peak:
            label = "eaten"
        else:
            label = "pull"
        return WallResolution(ts_ms=t, symbol=self.symbol, price=round(key * self.tick, 6),
                              side=W.side, resolution=label, peak_size=W.peak,
                              exec_total=W.exec_total, refills=W.refills,
                              lifetime_ms=t - W.born_ts)
