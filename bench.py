"""Shared reduction for the renderers' `--bench` modes (bridge.py and the matplotlib
renderer). Pure functions over collected samples; nothing here measures.

Samples:
  window_ms   measured wall time (warm-up excluded)
  paints      frames rendered in that window
  rafs        display frames observed (browser only; 0 for matplotlib)
  render_ms   per-frame render cost
  lat         per new book: [ingest→paint, ingest→sent, sent→recv, recv→paint]
              (matplotlib has no transport, so its rows carry ingest→paint only)
"""

from __future__ import annotations

import math

LAT_COLS = ("ingest_to_paint", "ingest_to_sent", "sent_to_recv", "recv_to_paint")


def new_samples() -> dict:
    return {"window_ms": 0.0, "paints": 0, "rafs": 0, "render_ms": [], "lat": []}


def pct(xs, q):
    """Nearest-rank percentile of a non-empty list."""
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, math.ceil(q / 100.0 * len(xs)) - 1))]


def summary(b: dict) -> dict:
    """Reduce samples to the reported numbers: FPS, frame time p50/p95, latency p50/p95."""
    secs = b["window_ms"] / 1000.0
    out = {"seconds": round(secs, 1), "paints": b["paints"],
           "render_fps": round(b["paints"] / secs, 2) if secs else None,
           "frames_measured": len(b["render_ms"]), "latency_samples": len(b["lat"])}
    if b["rafs"]:
        out["raf_fps"] = round(b["rafs"] / secs, 1)
    if b["render_ms"]:
        out["render_ms_p50"] = round(pct(b["render_ms"], 50), 2)
        out["render_ms_p95"] = round(pct(b["render_ms"], 95), 2)
    for i, name in enumerate(LAT_COLS):
        col = [row[i] for row in b["lat"] if len(row) > i]
        if col:
            out[f"{name}_ms_p50"] = round(pct(col, 50), 1)
            out[f"{name}_ms_p95"] = round(pct(col, 95), 1)
    return out
