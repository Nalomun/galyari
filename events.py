"""Research-grade event store + forward-outcome labeler (GALYARI_EDGE_PLAN.md Phase E1/E2).

This is the bridge from *viz* to *edge*: it turns the detectors into a stream of discrete,
timestamped, point-in-time-correct events, then joins each to subsequent price so every
signal is born measurable. Same skepticism as the GICS/Slandle pipelines — append-only,
no look-ahead, and the labeler is a *separate offline pass* from the extractor.

Two stages:

  E1  EventExtractor — replays a tape through the existing `MicrostructureAnalyzer` and the
      Phase-B `LevelTracker`, emitting an `Event` per:
        - level-state transition  (WATCHING→VACUUM, →BROKEN, …)   from levels.py
        - pull                    (a wall vanishing un-eaten)       from microstructure.py
        - iceberg *appearance*    (deduped: only when first detected)
      Each event carries the book context at that instant: mid, spread, imbalance. Slots for
      runway / venue-conviction / absorption are present but None until Phase C/D fill them.

  E2  label_events — joins each event to the symbol's forward mid: return (bps) at 30s/2m/5m,
      plus max up/down excursion (MFE/MAE, direction-agnostic) over the longest horizon.
      Labeling is per (tape, symbol) so returns never cross a session/stream break.

CLI:
    python events.py                      # all recordings/*.jsonl
    python events.py recordings/X.jsonl --out events_labeled.jsonl --summary SIGNALS_PREVIEW.md

The summary groups events by (kind, label, side) and reports forward-return stats — a
*preview* of Phase E3. The honesty gate (permutation test, VISUAL-ONLY vs MEASURED-EDGE) is
deliberately left to E3; this stage only produces the labels E3 will test.
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from orderbook import parse_books, Trade
from microstructure import MicrostructureAnalyzer
from levels import LevelTracker
from walls import WallTracker
from runway import runway_score, runway_label

LIVE_TICK = 0.01
LIVE_WINDOW = 240               # MicrostructureAnalyzer frame window (matches the renderer)
LEVEL_WINDOW_MS = 30_000
HORIZONS_MS = (30_000, 120_000, 300_000)   # 30s / 2m / 5m, per the plan
SESSION_GAP_MS = 5_000


# --------------------------------------------------------------------------- #
# E1 — event schema + extractor
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    ts_ms: int
    symbol: str
    kind: str            # 'level_state' | 'pull' | 'iceberg'
    label: str           # level label (HOD/round/…) or '' for pull/iceberg
    side: str            # 'resistance'|'support'|'at'|'bid'|'ask'|''
    price: float
    # point-in-time book context (no look-ahead)
    mid: float
    spread: float
    imbalance: float
    # event-specific payload + forward-compatible slots (filled in later phases)
    detail: dict = field(default_factory=dict)
    # forward-outcome labels, attached by E2 (None until labeled)
    labels: dict | None = None

    def to_row(self) -> dict:
        row = {"ts_ms": self.ts_ms, "symbol": self.symbol, "kind": self.kind,
               "label": self.label, "side": self.side, "price": round(self.price, 6),
               "mid": round(self.mid, 6), "spread": round(self.spread, 6),
               "imbalance": round(self.imbalance, 6), "detail": self.detail}
        if self.labels is not None:
            row["labels"] = self.labels
        return row


def book_imbalance(book) -> float | None:
    """(bid_vol − ask_vol) / total over all visible levels — identical to the renderer's
    `Heatmap.imbalance`, kept here so the event store and the screen agree."""
    if book is None or not book.bids or not book.asks:
        return None
    bv = sum(l.volume for l in book.bids)
    av = sum(l.volume for l in book.asks)
    tot = bv + av
    return (bv - av) / tot if tot else None


class EventExtractor:
    """Per-symbol detector harness. Construct one per symbol; feed `process(book, trades)`
    in tape order; collect the returned events."""

    def __init__(self, tick: float = LIVE_TICK, window: int = LIVE_WINDOW,
                 level_window_ms: int = LEVEL_WINDOW_MS, auto_levels: bool = True):
        self.micro = MicrostructureAnalyzer(tick=tick, window=window)
        self.levels = LevelTracker(tick=tick, window_ms=level_window_ms, auto=auto_levels)
        self.walls = WallTracker(tick=tick)
        self._active_icebergs: set[int] = set()
        self.tick = tick

    def _key(self, price: float) -> int:
        return int(round(price / self.tick))

    def process(self, book, trades, ts_ms: int) -> list[Event]:
        ms = self.micro.update(book, trades)
        statuses, level_events = self.levels.update(book, trades, ts_ms, micro=ms)
        wall_resolutions = self.walls.update(book, trades, ts_ms, micro=ms)

        mid = book.mid
        if mid is None:
            return []                      # one-sided book → no usable context/baseline
        spread = book.spread if book.spread is not None else 0.0
        imb = book_imbalance(book) or 0.0
        out: list[Event] = []

        def ctx_at(price):
            """The conditioning context an event fired in — for Phase E3 conditional edge
            (e.g. PULL @ runway:clear vs @ runway:blocked, or co-located with a VACUUM level)."""
            c = {}
            rs = runway_score(book, price, "auto", n_ticks=self.levels.runway_ticks,
                              tick=self.tick)
            if rs is not None:
                c["ctx_runway"] = rs.runway
                c["ctx_runway_label"] = runway_label(rs)
            near, best = None, 1e9
            for s in statuses:                       # nearest *active* level within 3 ticks
                if s.state.value == "watching":
                    continue
                d = abs(s.loi.price - price) / self.tick
                if d < best and d <= 3:
                    best, near = d, s
            c["ctx_level"] = near.state.value if near else "none"
            c["ctx_imb"] = ("bid_heavy" if imb > 0.1 else
                            "ask_heavy" if imb < -0.1 else "balanced")
            return c

        def mk(kind, label, side, price, detail, with_ctx=True):
            if with_ctx:
                detail = {**detail, **ctx_at(price)}
            return Event(ts_ms=ts_ms, symbol=book.symbol, kind=kind, label=label,
                         side=side, price=price, mid=mid, spread=spread,
                         imbalance=imb, detail=detail)

        # level-state transitions
        for le in level_events:
            out.append(mk("level_state", le.label, _le_side(le, mid),
                          le.price, {"old_state": le.old_state,
                                     "new_state": le.new_state,
                                     "resting_size": le.resting_size,
                                     "refresh_count": le.refresh_count,
                                     "volume_through": le.volume_through,
                                     "beyond_thickness": le.beyond_thickness,
                                     "runway": round(le.runway, 4),
                                     "ctx_imb": ("bid_heavy" if imb > 0.1 else
                                                 "ask_heavy" if imb < -0.1 else "balanced")},
                          with_ctx=False))

        # wall lifecycle resolutions (C3): the unified REFRESH/PULL/EATEN terminal label
        for r in wall_resolutions:
            out.append(mk("wall_resolution", r.resolution, r.side, r.price,
                          {"resolution": r.resolution, "peak_size": r.peak_size,
                           "exec_total": r.exec_total, "refills": r.refills,
                           "lifetime_ms": r.lifetime_ms}))

        # raw pulls (kept alongside C3: discrete per-frame, finer-grained than the lifecycle)
        for p in ms.pulls:
            out.append(mk("pull", "", p.side, p.price, {"prev_size": p.prev_size}))

        # iceberg appearances (deduped — only emit when a level first becomes an iceberg)
        now = set()
        for ic in ms.icebergs:
            k = self._key(ic.price)
            now.add(k)
            if k not in self._active_icebergs:
                out.append(mk("iceberg", "", ic.side, ic.price,
                              {"executed": ic.executed, "displayed": ic.displayed,
                               "refills": ic.refills}))
        self._active_icebergs = now
        return out


def _le_side(le, mid) -> str:
    if le.price > mid:
        return "resistance"
    if le.price < mid:
        return "support"
    return "at"


def extract_from_tape(path: str) -> tuple[list[Event], dict]:
    """Replay one tape. Returns (events, mid_series) where mid_series is
    {symbol: sorted [(ts_ms, mid)]} for the E2 labeler."""
    extractors: dict[str, EventExtractor] = {}
    pending: dict[str, list] = defaultdict(list)
    events: list[Event] = []
    mids: dict[str, list] = defaultdict(list)

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = rec.get("t")
            if kind == "trade":
                try:
                    s = rec.get("symbol", "")
                    pending[s].append(Trade(s, int(rec["ts_ms"]), float(rec["price"]),
                                            int(rec["size"]), int(rec.get("side", 0))))
                except (KeyError, TypeError, ValueError):
                    pass
                continue
            if kind not in ("book", "sim_book"):
                continue
            books = (parse_books(rec.get("msg", {})) if kind == "book"
                     else _sim_books(rec))
            for bk in books:
                ts = rec.get("ts_ms", bk.ts_ms) if kind == "sim_book" else bk.ts_ms
                ex = extractors.get(bk.symbol)
                if ex is None:
                    ex = extractors[bk.symbol] = EventExtractor()
                events.extend(ex.process(bk, pending.pop(bk.symbol, []), ts))
                if bk.mid is not None:
                    mids[bk.symbol].append((ts, bk.mid))

    for s in mids:
        mids[s].sort()
    return events, mids


def _sim_books(rec):
    from orderbook import Book, Level
    bids = tuple(Level(p, v) for p, v in rec.get("bids", ()))
    asks = tuple(Level(p, v) for p, v in rec.get("asks", ()))
    if bids or asks:
        return [Book(rec.get("symbol", "SIM"), rec.get("ts_ms", 0), bids, asks)]
    return []


# --------------------------------------------------------------------------- #
# E2 — forward-outcome labeler
# --------------------------------------------------------------------------- #
def _fwd_mid(times, mids, ts, horizon_ms):
    """Mid at the first frame with ts >= target, within one horizon of slack (else None)."""
    target = ts + horizon_ms
    i = bisect.bisect_left(times, target)
    if i >= len(times) or times[i] - target > horizon_ms:
        return None
    return mids[i]


def _excursion(times, mids, ts, max_horizon_ms, base):
    """Max up/down excursion of the mid over [ts, ts+max_horizon], in bps from `base`."""
    lo = bisect.bisect_left(times, ts)
    hi = bisect.bisect_right(times, ts + max_horizon_ms)
    window = mids[lo:hi]
    if not window or base <= 0:
        return None, None
    up = (max(window) - base) / base * 1e4
    dn = (min(window) - base) / base * 1e4
    return round(up, 2), round(dn, 2)


def label_events(events, mid_series):
    """Attach forward-return labels to each event in place (per its symbol's mid series).

    Returns the count of events that got at least one horizon labeled."""
    max_h = max(HORIZONS_MS)
    labeled = 0
    for ev in events:
        series = mid_series.get(ev.symbol)
        if not series or ev.mid <= 0:
            ev.labels = {}
            continue
        times = [t for t, _ in series]
        mids = [m for _, m in series]
        lab = {}
        for h in HORIZONS_MS:
            fm = _fwd_mid(times, mids, ev.ts_ms, h)
            if fm is not None:
                lab[f"ret_bps_{h // 1000}s"] = round((fm - ev.mid) / ev.mid * 1e4, 2)
        up, dn = _excursion(times, mids, ev.ts_ms, max_h, ev.mid)
        if up is not None:
            lab["mfe_up_bps"] = up
            lab["mae_dn_bps"] = dn
        ev.labels = lab
        if lab:
            labeled += 1
    return labeled


# --------------------------------------------------------------------------- #
# summary (E3 preview — descriptive only, no significance test yet)
# --------------------------------------------------------------------------- #
def _group_key(ev):
    if ev.kind == "level_state":
        return (f"level→{ev.detail.get('new_state', '?')}", ev.label, ev.side)
    if ev.kind == "wall_resolution":
        return (f"wall:{ev.label}", "", ev.side)     # refresh / pull / eaten
    return (ev.kind, "", ev.side)


def build_summary(events):
    H = "ret_bps_30s"
    groups = defaultdict(list)
    for ev in events:
        if ev.labels and H in ev.labels:
            groups[_group_key(ev)].append(ev)
    L = ["# SIGNALS_PREVIEW.md — Phase E2 forward-outcome labels\n",
         "> **Descriptive preview only — NOT the E3 honesty gate.** These are raw forward "
         "mid-returns grouped by event type; no permutation test has been run, so **every "
         "signal here is still VISUAL-ONLY**. A non-zero mean below is necessary but not "
         "sufficient for edge. Returns are in basis points of the event-instant mid.\n",
         f"Horizons: {', '.join(str(h // 1000) + 's' for h in HORIZONS_MS)}. "
         "`P(up@30s)` is the fraction with positive 30s return.\n",
         "| event (kind / new-state) | level | side | n | mean 30s | median 30s | "
         "mean 2m | mean 5m | P(up@30s) |",
         "|---|---|---|---:|---:|---:|---:|---:|---:|"]

    def mean_at(evs, key):
        vals = [e.labels[key] for e in evs if key in e.labels]
        return statistics.fmean(vals) if vals else float("nan")

    def med_at(evs, key):
        vals = [e.labels[key] for e in evs if key in e.labels]
        return statistics.median(vals) if vals else float("nan")

    for (kind, label, side) in sorted(groups, key=lambda k: -len(groups[k])):
        evs = groups[(kind, label, side)]
        r30 = [e.labels[H] for e in evs if H in e.labels]
        pup = sum(1 for v in r30 if v > 0) / len(r30) if r30 else float("nan")
        L.append(
            f"| {kind} | {label or '—'} | {side or '—'} | {len(evs)} | "
            f"{_f(mean_at(evs, H))} | {_f(med_at(evs, H))} | "
            f"{_f(mean_at(evs, 'ret_bps_120s'))} | {_f(mean_at(evs, 'ret_bps_300s'))} | "
            f"{_f(100 * pup, 0)}% |")
    L.append("\n_Next: Phase E3 — condition each row on context (e.g. PULL at a VACUUM level "
             "vs a DEFENDED one) and run the permutation test against shuffled labels before "
             "anything is relabeled MEASURED-EDGE._\n")
    return "\n".join(L)


def _f(x, nd=1):
    if x != x:
        return "n/a"
    return f"{x:,.{nd}f}"


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Phase E1/E2 event store + labeler.")
    ap.add_argument("tapes", nargs="*", help="recording JSONL (default: recordings/*.jsonl)")
    ap.add_argument("--out", default="events_labeled.jsonl", help="labeled event store path")
    ap.add_argument("--summary", default="SIGNALS_PREVIEW.md", help="summary markdown path")
    args = ap.parse_args()

    paths = args.tapes or sorted(glob.glob("recordings/*.jsonl"))
    if not paths:
        raise SystemExit("no tapes found (recordings/*.jsonl)")

    all_events = []
    for path in paths:
        events, mids = extract_from_tape(path)
        label_events(events, mids)         # label within this tape's own session
        all_events.extend(events)
        print(f"[events] {path}: {len(events)} events")

    with open(args.out, "w") as f:
        for ev in all_events:
            f.write(json.dumps(ev.to_row(), separators=(",", ":")) + "\n")
    with open(args.summary, "w") as f:
        f.write(build_summary(all_events))
    print(f"[events] wrote {len(all_events)} events → {args.out}; summary → {args.summary}")


if __name__ == "__main__":
    main()
