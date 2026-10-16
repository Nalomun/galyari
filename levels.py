"""Level-centric layer (GALYARI_EDGE_PLAN.md Phase B).

Converts "read the heatmap" into "the level you marked is DEFENDED and refreshing 3×" or
"the level just flipped to VACUUM." A *level of interest* is a price (or tight band) the
trader cares about; this module tracks each one's live state from the snapshot diff + the
derived trade stream and classifies it.

A pure consumer of the stable core (`orderbook.Book`/`Trade`) — stdlib only, no matplotlib,
no numpy — same seam discipline as `microstructure.py`. Feed it one `(book, trades, ts_ms)`
per tick via `LevelTracker.update()`; optionally pass the `MicroState` from the analyzer to
attribute pulls/icebergs to levels.

Two deliberate design choices, both grounded in Phase A (`VALIDATION.md`):
  - **Windows are wall-clock time (`window_ms`), not frame counts.** The feed's median Δt is
    ~1–1.5s and bursty, so a frame-count window drifts with cadence (the live analyzer's
    240-frame window is ~4–6 min). Time windows are stable across the sample-rate jitter.
  - **Every state change emits a discrete, timestamped `LevelEvent`.** Those events are the
    unit Phase E measures for edge.

State machine (per the plan):
  WATCHING   — tracked, nothing decisive happening.
  DEFENDED   — resting size keeps refreshing under aggression (iceberg/absorption at the
               level). For a breakout this is the *fade/grind* read.
  ABSORBING  — heavy volume into the level, little price progress, not yet resolved.
  VACUUM     — thin/absent book just beyond the level. The *run* read (precursor to C1).
  BROKEN     — cleared and price accepted through.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from runway import runway_score


class LevelState(str, Enum):
    WATCHING = "watching"
    DEFENDED = "defended"
    ABSORBING = "absorbing"
    VACUUM = "vacuum"
    BROKEN = "broken"


@dataclass(frozen=True)
class LevelOfInterest:
    """A price the trader cares about. `band` is the half-width (in price) that counts as
    'at' the level — defaults to one tick."""
    price: float
    label: str               # 'manual', 'HOD', 'LOD', 'VWAP', 'round', 'HVN', 'LVN'
    source: str              # 'manual' | 'auto'
    band: float


@dataclass(frozen=True)
class LevelStatus:
    """The live read of one level at a tick."""
    loi: LevelOfInterest
    state: LevelState
    side: str                # 'resistance' (above mid) | 'support' (below) | 'at'
    resting_size: float      # displayed size within the band, now
    refresh_count: int       # deplete→recover cycles under execution, in window
    volume_through: float    # traded volume within the band, in window
    signed_volume: float     # buy(+) − sell(−) volume within the band, in window
    pulls: int               # pull events attributed to the band, in window
    beyond_thickness: float  # displayed size in the N ticks just beyond the level
    runway: float            # C1 runway score 0..1 (1 = clear lane beyond, 0 = walled)
    distance_ticks: float    # |mid − price| in ticks (nan if no mid)


@dataclass(frozen=True)
class LevelEvent:
    """A state transition — the discrete, point-in-time record Phase E consumes."""
    ts_ms: int
    symbol: str
    price: float
    label: str
    old_state: str
    new_state: str
    mid: float
    resting_size: float
    refresh_count: int
    volume_through: float
    signed_volume: float
    beyond_thickness: float
    runway: float = 0.0


# --------------------------------------------------------------------------- #
# per-level rolling state (time-windowed)
# --------------------------------------------------------------------------- #
class _LevelState:
    __slots__ = ("sizes", "execs", "refills", "mids", "last_size", "depleted",
                 "last_exec_ms", "pulls", "state", "broken_since")

    def __init__(self):
        self.sizes: deque = deque()     # (ts, size) within band
        self.execs: deque = deque()     # (ts, signed_size)
        self.refills: deque = deque()   # (ts,) recoveries that followed execution
        self.mids: deque = deque()      # (ts, mid)
        self.pulls: deque = deque()     # (ts,) pull events at this band
        self.last_size = 0.0
        self.depleted = False
        self.last_exec_ms = -10 ** 18
        self.state = LevelState.WATCHING
        self.broken_since: int | None = None

    def empty(self) -> bool:
        return not (self.sizes or self.execs or self.pulls)


class LevelTracker:
    """Tracks levels of interest for one symbol and classifies each per tick.

    Construct one per focused symbol; call `reset()` on a live symbol switch (mirrors
    `MicrostructureAnalyzer`)."""

    def __init__(self, tick: float = 0.01, window_ms: int = 30_000,
                 # how close to the mid a level must be to get an active read
                 active_ticks: int = 80,
                 # DEFENDED: refresh cycles needed; ABSORBING: volume needed
                 refresh_min: int = 2, absorb_mult: float = 6.0,
                 # VACUUM (C1): runway score (0..1) beyond the level at/above this →
                 # the lane is judged over `runway_ticks`, normalized to the name's depth
                 vacuum_runway: float = 0.6, runway_ticks: int = 10,
                 # BROKEN: mid must clear the band by this many ticks and hold
                 broken_ticks: float = 1.0, broken_hold_ms: int = 1_500,
                 # auto-level knobs
                 round_increment: float | None = None, hvn_top: int = 2,
                 auto: bool = True):
        self.tick = tick
        self.window_ms = window_ms
        self.active_ticks = active_ticks
        self.refresh_min = refresh_min
        self.absorb_mult = absorb_mult
        self.vacuum_runway = vacuum_runway
        self.runway_ticks = runway_ticks
        self.broken_ticks = broken_ticks
        self.broken_hold_ms = broken_hold_ms
        self.round_increment = round_increment
        self.hvn_top = hvn_top
        self.auto = auto

        self.symbol = ""
        self._levels: dict[int, _LevelState] = {}      # price-key -> rolling state
        self._manual: dict[int, LevelOfInterest] = {}  # price-key -> manual LOI

        # session aggregates (for auto levels) — not windowed
        self._hod: float | None = None
        self._lod: float | None = None
        self._vwap_pv = 0.0
        self._vwap_v = 0.0
        self._profile: dict[int, float] = {}           # price-key -> traded volume (session)

    # --- key helpers --------------------------------------------------------
    def _key(self, price: float) -> int:
        return int(round(price / self.tick))

    def _price(self, key: int) -> float:
        return round(key * self.tick, 6)

    # --- lifecycle ----------------------------------------------------------
    def reset(self) -> None:
        self.symbol = ""
        self._levels.clear()
        self._manual.clear()
        self._hod = self._lod = None
        self._vwap_pv = self._vwap_v = 0.0
        self._profile.clear()

    # --- manual levels ------------------------------------------------------
    def add_manual(self, price: float, label: str = "manual",
                   band: float | None = None) -> LevelOfInterest:
        loi = LevelOfInterest(price=self._price(self._key(price)), label=label,
                              source="manual", band=band if band is not None else self.tick)
        self._manual[self._key(price)] = loi
        return loi

    def remove_manual(self, price: float) -> None:
        self._manual.pop(self._key(price), None)

    def clear_manual(self) -> None:
        self._manual.clear()

    # --- session aggregates -------------------------------------------------
    @property
    def vwap(self) -> float | None:
        return self._vwap_pv / self._vwap_v if self._vwap_v > 0 else None

    @property
    def hod(self) -> float | None:
        return self._hod

    @property
    def lod(self) -> float | None:
        return self._lod

    def _ingest_trades_session(self, trades) -> None:
        for tr in trades:
            self._hod = tr.price if self._hod is None else max(self._hod, tr.price)
            self._lod = tr.price if self._lod is None else min(self._lod, tr.price)
            self._vwap_pv += tr.price * tr.size
            self._vwap_v += tr.size
            k = self._key(tr.price)
            self._profile[k] = self._profile.get(k, 0.0) + tr.size

    # --- auto level generation ---------------------------------------------
    def _round_levels(self, mid: float) -> list[float]:
        inc = self.round_increment
        if inc is None:
            # scale the round-number grid to price: $0.10 under $5, $1 under $50, else $5
            inc = 0.10 if mid < 5 else 1.0 if mid < 50 else 5.0
        base = round(mid / inc) * inc
        return [round(base + i * inc, 6) for i in (-1, 0, 1)]

    def _hvn_lvn(self) -> tuple[list[float], list[float]]:
        """High/low-volume nodes from the session trade profile."""
        if len(self._profile) < 3:
            return [], []
        items = sorted(self._profile.items())          # by price-key
        keys = [k for k, _ in items]
        vols = {k: v for k, v in items}
        hvn_keys = sorted(vols, key=lambda k: -vols[k])[:self.hvn_top]
        # LVN: the lowest-volume key strictly between the two largest HVNs (a gap to run)
        lvn = []
        if len(hvn_keys) >= 2:
            lo, hi = sorted(hvn_keys[:2])
            between = [k for k in keys if lo < k < hi]
            if between:
                lvn_key = min(between, key=lambda k: vols[k])
                lvn = [self._price(lvn_key)]
        return [self._price(k) for k in hvn_keys], lvn

    def _auto_lois(self, mid: float) -> list[LevelOfInterest]:
        out = []
        b = self.tick
        if self._hod is not None:
            out.append(LevelOfInterest(self._price(self._key(self._hod)), "HOD", "auto", b))
        if self._lod is not None:
            out.append(LevelOfInterest(self._price(self._key(self._lod)), "LOD", "auto", b))
        vw = self.vwap
        if vw is not None:
            out.append(LevelOfInterest(self._price(self._key(vw)), "VWAP", "auto", b))
        for rp in self._round_levels(mid):
            out.append(LevelOfInterest(self._price(self._key(rp)), "round", "auto", b))
        hvn, lvn = self._hvn_lvn()
        for hp in hvn:
            out.append(LevelOfInterest(hp, "HVN", "auto", b))
        for lp in lvn:
            out.append(LevelOfInterest(lp, "LVN", "auto", b))
        return out

    # --- book/trade measurement helpers ------------------------------------
    def _band_size(self, book, price: float, band: float) -> float:
        return sum(v for p, v in book.levels() if abs(p - price) <= band + 1e-9)

    @staticmethod
    def _typical_level_size(book) -> float:
        sizes = sorted(v for _, v in book.levels() if v > 0)
        if not sizes:
            return 0.0
        return sizes[len(sizes) // 2]     # median

    # --- main update --------------------------------------------------------
    def update(self, book, trades=(), ts_ms: int | None = None, micro=None
               ) -> tuple[tuple[LevelStatus, ...], tuple[LevelEvent, ...]]:
        """Advance one tick. Returns (statuses, transition_events).

        `ts_ms` defaults to the book's timestamp. `micro` (a `MicroState`) is optional; when
        present, its pulls are attributed to nearby levels for context and events."""
        if book is None or not (book.bids or book.asks):
            return (), ()
        if book.symbol:
            self.symbol = book.symbol
        t = ts_ms if ts_ms is not None else book.ts_ms
        mid = book.mid
        cutoff = t - self.window_ms

        self._ingest_trades_session(trades)
        typical = self._typical_level_size(book)

        # which levels to track this tick: manual ∪ auto, near the mid
        lois: dict[int, LevelOfInterest] = {}
        for k, loi in self._manual.items():
            lois[k] = loi
        if self.auto and mid is not None:
            for loi in self._auto_lois(mid):
                lois.setdefault(self._key(loi.price), loi)   # manual wins on a tie

        # fold per-band displayed size + execution into rolling state
        for k, loi in lois.items():
            L = self._levels.get(k)
            if L is None:
                L = self._levels[k] = _LevelState()
            size = self._band_size(book, loi.price, loi.band)
            # deplete→recover refill bookkeeping (mirrors microstructure, but band-scoped)
            if L.last_size > 0 and size <= L.last_size * 0.6:
                L.depleted = True
            elif L.depleted and size >= L.last_size * 1.3:
                if t - L.last_exec_ms <= self.window_ms // 4:
                    L.refills.append((t,))
                L.depleted = False
            L.last_size = size
            L.sizes.append((t, size))
            if mid is not None:
                L.mids.append((t, mid))

        # attribute trades to bands. Each exec is (ts, signed_size, gross_size): signed
        # carries the inferred aggressor (+buy/−sell, 0 when ambiguous), gross is the raw
        # traded size used for volume-through.
        for tr in trades:
            tp = self._price(self._key(tr.price))
            signed = tr.size if tr.side > 0 else -tr.size if tr.side < 0 else 0
            for kk, loi in lois.items():
                if abs(tp - loi.price) <= loi.band + 1e-9:
                    L = self._levels.get(kk)
                    if L is not None:
                        L.execs.append((t, signed, tr.size))
                        L.last_exec_ms = t

        # attribute pulls from MicroState (optional)
        if micro is not None:
            for p in getattr(micro, "pulls", ()):
                k = self._key(p.price)
                for kk, loi in lois.items():
                    if abs(self._price(k) - loi.price) <= loi.band + 1e-9:
                        L = self._levels.get(kk)
                        if L is not None:
                            L.pulls.append((t,))

        self._prune(cutoff)

        statuses, events = [], []
        for k, loi in lois.items():
            L = self._levels[k]
            st, ev = self._classify(L, loi, book, mid, t, typical)
            statuses.append(st)
            if ev is not None:
                events.append(ev)

        self._gc(set(lois))
        statuses.sort(key=lambda s: s.distance_ticks if s.distance_ticks == s.distance_ticks
                      else 1e18)
        return tuple(statuses), tuple(events)

    # --- classification -----------------------------------------------------
    def _classify(self, L, loi, book, mid, t, typical):
        side = "at"
        dist = float("nan")
        if mid is not None:
            if loi.price > mid + loi.band:
                side = "resistance"
            elif loi.price < mid - loi.band:
                side = "support"
            dist = abs(mid - loi.price) / self.tick

        resting = L.sizes[-1][1] if L.sizes else 0.0
        refills = len(L.refills)
        gross = sum(g for (_, _, g) in L.execs)
        signed = sum(s for (_, s, _) in L.execs)
        pulls = len(L.pulls)

        # C1 runway beyond the level (None when the level sits at the mid). `beyond` is the
        # raw lane size; `runway` is the normalized 0..1 score that also drives VACUUM.
        rs = runway_score(book, loi.price, side, n_ticks=self.runway_ticks,
                          tick=self.tick, typical=typical) if side != "at" else None
        beyond = rs.beyond_size if rs else 0.0
        runway = rs.runway if rs else 0.0

        new = self._state_of(L, loi, side, dist, mid, resting, refills, gross, runway,
                              typical, t)

        old = L.state
        ev = None
        if new != old:
            L.state = new
            ev = LevelEvent(ts_ms=t, symbol=self.symbol, price=loi.price, label=loi.label,
                            old_state=old.value, new_state=new.value,
                            mid=mid if mid is not None else float("nan"),
                            resting_size=resting, refresh_count=refills,
                            volume_through=gross, signed_volume=signed,
                            beyond_thickness=beyond, runway=runway)

        return (LevelStatus(loi=loi, state=new, side=side, resting_size=resting,
                            refresh_count=refills, volume_through=gross,
                            signed_volume=signed, pulls=pulls, beyond_thickness=beyond,
                            runway=runway, distance_ticks=dist), ev)

    def _state_of(self, L, loi, side, dist, mid, resting, refills, gross, runway,
                  typical, t):
        # too far from the action to read meaningfully
        if dist != dist or dist > self.active_ticks:
            L.broken_since = None
            return LevelState.WATCHING

        # BROKEN: price has crossed the level and accepted on the far side. Highest priority
        # — a broken level is broken regardless of residual size or volume.
        if self._check_broken(L, mid, loi, t):
            return LevelState.BROKEN

        # DEFENDED: size present and refreshing under aggression (the iceberg/absorption read)
        if resting > 0 and refills >= self.refresh_min and gross > 0:
            return LevelState.DEFENDED

        # ABSORBING: heavy volume into the level, little progress, not (yet) refreshing/broken
        if typical > 0 and gross >= self.absorb_mult * typical and resting > 0:
            return LevelState.ABSORBING

        # VACUUM: clear runway beyond the level (the run read), normalized to the name's depth
        if side in ("resistance", "support") and runway >= self.vacuum_runway:
            return LevelState.VACUUM

        return LevelState.WATCHING

    def _check_broken(self, L, mid, loi, t) -> bool:
        """True once the mid has *crossed* the level and held beyond it by the margin for
        ≥ broken_hold_ms. 'Crossed' requires an earlier mid on the opposite side within the
        window, so a level that was always on one side is never spuriously 'broken'."""
        if mid is None:
            L.broken_since = None
            return False
        margin = self.broken_ticks * self.tick
        above = mid >= loi.price + margin
        below = mid <= loi.price - margin
        if not (above or below):
            L.broken_since = None
            return False
        past = [m for (_, m) in L.mids]
        crossed = (above and any(m < loi.price for m in past)) or \
                  (below and any(m > loi.price for m in past))
        if not crossed:
            L.broken_since = None
            return False
        if L.broken_since is None:
            L.broken_since = t
        return t - L.broken_since >= self.broken_hold_ms

    # --- pruning ------------------------------------------------------------
    def _prune(self, cutoff: int) -> None:
        for L in self._levels.values():
            for dq in (L.sizes, L.execs, L.refills, L.mids, L.pulls):
                while dq and dq[0][0] <= cutoff:
                    dq.popleft()

    def _gc(self, active_keys: set) -> None:
        dead = [k for k, L in self._levels.items()
                if L.empty() and k not in active_keys and k not in self._manual]
        for k in dead:
            del self._levels[k]
