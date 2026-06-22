"""Append-only, immutable event log of incoming frames (JSONL).

One JSON object per line, never rewritten — a faithful tape that `feeds.run_replay`
can play back through the exact same shared-state plumbing the live/sim feeds use.

Line schema (discriminated by "t"):
  {"t":"book",     "ts_ms":<int>, "msg":<raw schwab NASDAQ_BOOK msg>}
  {"t":"sim_book", "ts_ms":<int>, "symbol":..., "bids":[[p,v]...], "asks":[[p,v]...]}
  {"t":"trade",    "ts_ms":<int>, "symbol":..., "price":..., "size":..., "side":...}

Live books are recorded raw (faithful tape); trades are recorded as the derived
print (live trades are inferred from level-one — see DATA_SCHEMA.md), so replay
reproduces exactly what the renderer showed.
"""

from __future__ import annotations

import json
import os
import threading
import time


def default_path(symbol: str, when_ms: int) -> str:
    os.makedirs("recordings", exist_ok=True)
    # caller supplies the timestamp (we avoid wall-clock surprises in tests)
    return os.path.join("recordings", f"{symbol}_{when_ms}.jsonl")


class Recorder:
    """Thread-safe line-appender. Flushes each write so a kill leaves a valid tape."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._f = open(path, "a", buffering=1)  # line-buffered
        print(f"[rec] recording to {path}", flush=True)

    def _write(self, obj: dict) -> None:
        line = json.dumps(obj, separators=(",", ":"))
        with self._lock:
            self._f.write(line + "\n")

    def write_book(self, msg: dict) -> None:
        self._write({"t": "book", "ts_ms": _book_ts(msg), "msg": msg})

    def write_sim_book(self, book) -> None:
        self._write({"t": "sim_book", "ts_ms": book.ts_ms, "symbol": book.symbol,
                     "bids": [[l.price, l.volume] for l in book.bids],
                     "asks": [[l.price, l.volume] for l in book.asks]})

    def write_trade(self, trade) -> None:
        self._write({"t": "trade", "ts_ms": trade.ts_ms, "symbol": trade.symbol,
                     "price": trade.price, "size": trade.size, "side": trade.side})

    def write_event(self, kind: str, **fields) -> None:
        """Record a derived microstructure event (pull / iceberg). Ignored on replay."""
        self._write({"t": "event", "ts_ms": _now_ms(), "event": kind, **fields})

    def close(self) -> None:
        with self._lock:
            if not self._f.closed:
                self._f.close()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _book_ts(msg: dict) -> int:
    content = (msg or {}).get("content") or []
    if content:
        try:
            return int(content[0].get("BOOK_TIME", 0) or _now_ms())
        except (TypeError, ValueError):
            pass
    return _now_ms()
