"""Conditional-edge measurement + the honesty gate (GALYARI_EDGE_PLAN.md Phase E3).

The gate that turns *viz* into *edge*. Reads the labeled event store (`events.py` output) and,
for every signal, asks one question with the same skepticism applied to the GICS matrix:
**is the signal's forward return distinguishable from random?** — via a permutation test against
the symbol's own pool of event outcomes, so per-symbol drift and the coarse-tick flatness
(Phase A) are controlled for.

Two hard rules, both lifted straight from Phase A's findings:
  1. **Permutation, not eyeballing.** A non-zero mean is not edge. The observed group mean must
     beat a null built by drawing same-size random samples from the symbol's event pool.
  2. **Cross-universe consistency.** A result on one name is overfit until it holds on another
     (recall ATLN ask-pulls said +71% while GOOG said the opposite). A signal is only
     **MEASURED-EDGE** if ≥2 symbols have enough samples, share the sign, and at least one is
     significant. A single significant name is reported as a *lead*, still VISUAL-ONLY.
  3. **Multiple comparisons.** Every tested (signal, symbol, horizon) cell — unconditional and
     conditional — is one family. "Significant" means Holm-adjusted p < α across that whole
     family (family-wise error control). Benjamini–Hochberg q is reported alongside for
     reference only; it does not gate a verdict. N_PERM is set so the smallest attainable p,
     1/(N_PERM+1), is below α/m for the family sizes seen here — otherwise the correction
     could never be passed and "nothing survived" would be an artifact of resolution.

Everything that doesn't clear the bar stays **VISUAL-ONLY** — the honest default.

    python signals.py                       # reads events_labeled.jsonl
    python signals.py --events X.jsonl --out SIGNALS.md --horizon 120
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from collections import defaultdict

HORIZONS_S = (30, 120, 300)
MIN_N = 25            # minimum events for a (signal, symbol) cell to be testable
N_PERM = 10000        # resamples; min attainable p = 1/10001 ≈ 1.0e-4 < 0.05/138 ≈ 3.6e-4
ALPHA = 0.05
SEED = 0xED6E         # fixed: the gate must be reproducible


def signal_name(ev: dict) -> str:
    """The signal identity a trader would act on (level *labels* like HOD/round are folded in
    as context later; here we group by the structural event + side)."""
    kind = ev.get("kind")
    detail = ev.get("detail", {})
    if kind == "level_state":
        return f"level→{detail.get('new_state','?')} ({ev.get('side','?')})"
    if kind == "wall_resolution":
        return f"wall:{detail.get('resolution','?')} ({ev.get('side','?')})"
    return f"{kind} ({ev.get('side','?')})"


def cond_namer(dim: str):
    """A namer that splits pull/wall_resolution signals by the context they fired in
    (Phase E3 conditional edge). Returns None for events the dimension doesn't apply to."""
    def f(ev):
        if ev.get("kind") not in ("pull", "wall_resolution"):
            return None
        d = ev.get("detail", {})
        if dim == "runway":
            b = d.get("ctx_runway_label")
            return f"{signal_name(ev)} @runway:{b}" if b else None
        if dim == "level":
            b = d.get("ctx_level")
            return f"{signal_name(ev)} @lvl:{b}" if b and b != "none" else None
        return None
    return f


def _ret_key(h_s: int) -> str:
    return f"ret_bps_{h_s}s"


def load_events(path: str) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def permutation_p(observed_mean, pool, n, rng) -> float:
    """Two-sided p: P(|mean of n random draws from pool| >= |observed|). Controls for the
    symbol's own base drift, since the pool is that symbol's event outcomes."""
    if len(pool) < n or n == 0:
        return float("nan")
    base = statistics.fmean(pool)
    obs_dev = abs(observed_mean - base)
    hits = 0
    for _ in range(N_PERM):
        s = sum(rng.choice(pool) for _ in range(n)) / n
        if abs(s - base) >= obs_dev:
            hits += 1
    return (hits + 1) / (N_PERM + 1)      # +1: never report p=0


def measure(events, h_s: int, rng, namer=signal_name):
    """Return {signal: {symbol: {n, mean, p, base}}} for one horizon. `namer` maps an event to
    its signal name (or None to exclude it) — swap in `cond_namer(...)` for conditional edge."""
    key = _ret_key(h_s)
    # symbol event pool of forward returns at this horizon (for the null)
    pool = defaultdict(list)
    for ev in events:
        labs = ev.get("labels") or {}
        if key in labs:
            pool[ev["symbol"]].append(labs[key])
    # group returns by (signal, symbol)
    grp = defaultdict(lambda: defaultdict(list))
    for ev in events:
        labs = ev.get("labels") or {}
        if key not in labs:
            continue
        name = namer(ev)
        if name is None:
            continue
        grp[name][ev["symbol"]].append(labs[key])

    out = {}
    for sig, by_sym in grp.items():
        cells = {}
        for sym, rets in by_sym.items():
            if len(rets) < MIN_N:
                continue
            mean = statistics.fmean(rets)
            cells[sym] = {"n": len(rets), "mean": mean,
                          "base": statistics.fmean(pool[sym]),
                          "p": permutation_p(mean, pool[sym], len(rets), rng)}
        if cells:
            out[sig] = cells
    return out


def holm(ps: list[float]) -> list[float]:
    """Holm step-down adjusted p-values (family-wise error control), in input order."""
    m = len(ps)
    adj, run = [1.0] * m, 0.0
    for rank, i in enumerate(sorted(range(m), key=ps.__getitem__)):
        run = max(run, min(1.0, (m - rank) * ps[i]))
        adj[i] = run
    return adj


def bh(ps: list[float]) -> list[float]:
    """Benjamini–Hochberg adjusted p-values (q; false-discovery rate), in input order."""
    m = len(ps)
    adj, run = [1.0] * m, 1.0
    for k, i in enumerate(sorted(range(m), key=ps.__getitem__, reverse=True)):
        run = min(run, ps[i] * m / (m - k))
        adj[i] = run
    return adj


def adjust_family(per_h_list) -> int:
    """Attach `p_holm` and `q_bh` to every tested cell across all `{h: {sig: {sym: cell}}}`
    tables in `per_h_list`, treating them as one family. Returns the family size m."""
    cells = [c for per_h in per_h_list for by_sig in per_h.values()
             for by_sym in by_sig.values() for c in by_sym.values() if c["p"] == c["p"]]
    ps = [c["p"] for c in cells]
    for c, ph, q in zip(cells, holm(ps), bh(ps)):
        c["p_holm"], c["q_bh"] = ph, q
    return len(cells)


def verdict(cells_by_horizon, pkey: str = "p") -> tuple[str, str]:
    """Decide MEASURED-EDGE / lead / VISUAL-ONLY. Significance and sign-consistency must hold
    at the *same* horizon (no mixing across horizons), and ≥2 symbols must qualify.
    `pkey` picks which p decides significance (`p_holm` in the report)."""
    qualifying, sig_syms_at = [], {}
    flip_sig = single_sig = consistent_nosig = False

    for h_s, cells in cells_by_horizon.items():
        if not cells:
            continue
        syms = list(cells)
        sigs = [s for s in syms if cells[s][pkey] == cells[s][pkey] and cells[s][pkey] < ALPHA]
        signs = {1 if cells[s]["mean"] - cells[s]["base"] > 0 else -1 for s in syms}
        consistent = len(signs) == 1
        if len(syms) >= 2 and consistent and sigs:
            qualifying.append(h_s)
            sig_syms_at[h_s] = [(s, cells[s][pkey]) for s in sigs]
        if len(syms) >= 2 and sigs and not consistent:
            flip_sig = True
        if len(syms) == 1 and sigs:
            single_sig = True
        if len(syms) >= 2 and consistent and not sigs:
            consistent_nosig = True

    if qualifying:
        names = sorted({s for h in qualifying for s, _ in sig_syms_at[h]})
        return "MEASURED-EDGE", f"≥2 symbols agree in sign, significant at {qualifying}s on {names}"
    if flip_sig:
        return "VISUAL-ONLY", "significant but sign FLIPS across symbols (overfit — see Phase A)"
    if single_sig:
        return "VISUAL-ONLY", "single-name significant LEAD — unconfirmed across the universe"
    if consistent_nosig:
        return "VISUAL-ONLY", "sign consistent across symbols but not significant"
    return "VISUAL-ONLY", "not distinguishable from random under permutation"


def _measure_section(events, rng, namer):
    """{horizon: {signal: {symbol: cell}}} for one namer (no verdicts yet — those need the
    family-wide correction, which needs every section measured first)."""
    return {h: measure(events, h, rng, namer=namer) for h in HORIZONS_S}


def _render_section(per_h, detail=True):
    """Build (verdict_rows, detail_lines, verdicts_dict) from corrected cells."""
    signals = sorted({s for h in HORIZONS_S for s in per_h[h]})
    rows, det, verdicts = [], [], {}
    for sig in signals:
        cells_by_h = {h: per_h[h].get(sig, {}) for h in HORIZONS_S}
        v, why = verdict(cells_by_h, pkey="p_holm")
        verdicts[sig] = (v, why, cells_by_h)
        rows.append(f"| {sig} | **{v}** | {why} |")
        if detail:
            det.append(f"### {sig} — {v}\n")
            det.append("| horizon | symbol | n | mean | base | Δ | p | p (Holm) | q (BH) |")
            det.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
            for h in HORIZONS_S:
                for sym, c in sorted(cells_by_h[h].items()):
                    d = c["mean"] - c["base"]
                    mark = " *" if c.get("p_holm", 1.0) < ALPHA else ""
                    det.append(f"| {h}s | {sym} | {c['n']} | {c['mean']:+.1f} | "
                               f"{c['base']:+.1f} | {d:+.1f} | {_p(c['p'])} | "
                               f"{_p(c.get('p_holm', float('nan')))}{mark} | "
                               f"{_p(c.get('q_bh', float('nan')))} |")
            det.append("")
    return rows, det, verdicts


def _sample_lines(events, per_h_list):
    """Describe the sample as it is: which symbols reached a tested cell, and how many
    events those symbols contribute vs. how many sit in the store untested."""
    tested = {sym for per_h in per_h_list for by_sig in per_h.values()
              for by_sym in by_sig.values() for sym in by_sym}
    counts = defaultdict(int)
    for ev in events:
        counts[ev["symbol"]] += 1
    used = sum(counts[s] for s in tested)
    other = sorted(s for s in counts if s not in tested)
    line = (f"Events in store: {len(events):,}. Symbols with ≥1 tested cell: "
            f"{', '.join(f'{s} ({counts[s]:,})' for s in sorted(tested))} — "
            f"{used:,} events.")
    if other:
        line += (" Never reach a tested cell (below the sample floor): "
                 + ", ".join(f"{s} ({counts[s]:,})" for s in other) + ".")
    return [line + "\n"]


def _cell_counts(per_h_list, key):
    cells = [c for per_h in per_h_list for by_sig in per_h.values()
             for by_sym in by_sig.values() for c in by_sym.values()]
    return sum(1 for c in cells if c.get(key, 1.0) < ALPHA)


def build_report(events) -> str:
    rng = random.Random(SEED)
    # measure every section first (same RNG order as before: unconditional, runway, level)
    per_u = _measure_section(events, rng, signal_name)
    per_c = {dim: _measure_section(events, rng, cond_namer(dim)) for dim in ("runway", "level")}
    family = [per_u, *per_c.values()]
    m = adjust_family(family)
    floor = 1 / (N_PERM + 1)

    rows, det, verdicts = _render_section(per_u)

    L = ["# SIGNALS.md — Phase E3 conditional-edge gate\n",
         "> **The honesty gate.** Each signal's forward mid-return is tested against a null "
         f"built from its own symbol's event pool ({N_PERM:,} resamples drawn with replacement, "
         "two-sided). A signal is only **MEASURED-EDGE** if ≥2 symbols agree in sign at the "
         f"same horizon with ≥1 significant **after Holm correction across all {m} tested "
         f"cells** (family-wise α={ALPHA}). Single-name hits are **leads**, not edge. "
         "Default is **VISUAL-ONLY**.\n",
         f"Min samples per (signal, symbol) cell: {MIN_N}. Horizons: "
         f"{', '.join(str(h) + 's' for h in HORIZONS_S)}. Returns in bps of the event mid; "
         "`Δ` = group mean minus the symbol's base mean.\n",
         *_sample_lines(events, family),
         "## Multiple-comparison correction\n",
         f"| quantity | value |", "|---|---:|",
         f"| tested cells (family size m) | {m} |",
         f"| resamples per cell | {N_PERM:,} |",
         f"| smallest attainable p, 1/(N+1) | {floor:.6f} |",
         f"| Bonferroni threshold α/m | {ALPHA / m:.6f} |",
         f"| cells with raw p < {ALPHA} | {_cell_counts(family, 'p')} |",
         f"| cells with Holm-adjusted p < {ALPHA} (gates verdicts) | "
         f"{_cell_counts(family, 'p_holm')} |",
         f"| cells with BH q < {ALPHA} (reference only) | {_cell_counts(family, 'q_bh')} |",
         "",
         "## Verdicts (unconditional)\n",
         "| signal | verdict | why |", "|---|---|---|", *rows]

    # --- conditional edge: split pull/wall signals by the context they fired in ----------
    cond_rows, cond_details, cond_verdicts = [], [], {}
    for dim, title in (("runway", "by runway regime"), ("level", "by co-located level")):
        r, d, vv = _render_section(per_c[dim])
        if r:
            cond_rows.append(f"| _— {title} —_ | | |")
            cond_rows.extend(r)
            cond_details.extend(d)
            cond_verdicts.update(vv)

    L.append("\n## Conditional verdicts (Phase E3 — context-split)\n")
    L.append("> The plan's key hypothesis: a signal's edge may live only in *context* — "
             "'PULL at a VACUUM level' measured apart from 'PULL at a DEFENDED level'. Below, "
             "pull/wall-resolution signals are split by the **runway regime** and the "
             "**co-located level state** at the event instant. Same gate, same family-wide "
             "correction; conditioning fragments the sample, so most cells fall below the "
             f"{MIN_N}-sample floor and drop out (only testable ones shown).\n")
    if cond_rows:
        L += ["| conditional signal | verdict | why |", "|---|---|---|", *cond_rows]
    else:
        L.append("_No conditional sub-signal reached the sample floor on these tapes._")

    L.append("\n## Per-signal detail (unconditional)\n")
    L.append(f"`*` = Holm-adjusted p < {ALPHA}.\n")
    L.extend(det)
    if cond_details:
        L.append("## Per-signal detail (conditional)\n")
        L.extend(cond_details)

    edge = [s for s in verdicts if verdicts[s][0] == "MEASURED-EDGE"]
    cond_edge = [s for s in cond_verdicts if cond_verdicts[s][0] == "MEASURED-EDGE"]
    L.append("---\n")
    L.append(f"**Bottom line:** unconditional — {len(edge)} of {len(verdicts)} signals "
             f"MEASURED-EDGE ({', '.join(edge) if edge else '—'}); conditional — "
             f"{len(cond_edge)} of {len(cond_verdicts)} sub-signals MEASURED-EDGE "
             f"({', '.join(cond_edge) if cond_edge else '—'}). Everything else VISUAL-ONLY. "
             f"Significance is Holm-corrected across all {m} tested cells.\n")
    L.append("> **Limits the correction does not fix:**\n"
             "> - The null treats events as independent draws. They are not: forward windows "
             "overlap and level-state events repeat on the same price, so effective n is far "
             "below reported n and p-values are anti-conservative.\n"
             "> - The sample is one afternoon on three stocks — not a universe.\n"
             "> - Forward returns use mid fills; no spread or slippage.\n")
    L.append("\n**Next:** record more sessions and re-run the whole pipeline "
             "(`phase_a.py` → `events.py` → `signals.py`).")
    return "\n".join(L)


def _p(x):
    if x != x:
        return "n/a"
    return f"{x:.4f}"


def main():
    ap = argparse.ArgumentParser(description="Phase E3 conditional-edge permutation gate.")
    ap.add_argument("--events", default="events_labeled.jsonl", help="labeled event store")
    ap.add_argument("--out", default="SIGNALS.md", help="report output path")
    args = ap.parse_args()

    events = load_events(args.events)
    if not events:
        raise SystemExit(f"no events in {args.events} (run events.py first)")
    print(f"[signals] loaded {len(events)} events")
    report = build_report(events)
    with open(args.out, "w") as f:
        f.write(report)
    print(f"[signals] wrote {args.out}")


if __name__ == "__main__":
    main()
