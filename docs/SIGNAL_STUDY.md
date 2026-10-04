# Signal study: do Galyari's order-flow signals predict short-horizon returns?

**Result: no.** None of the 18 signals passes the test once p-values are corrected for the 138
comparisons made. Two individual test cells survive the correction, but each belongs to a
signal whose direction reverses between stocks.

This is a summary of method, sample and results. The study code and the recordings it ran on
are not published. The recordings are licensed market data.

## Question

When Galyari's detectors fire (a wall is pulled, eaten or refreshed; an iceberg is inferred; a
marked price level changes state), does the stock's mid-price move differently over the next
30 s, 2 min or 5 min than it does after the average event in that stock?

## Sample

| | |
|---|---|
| Source | Level 2 book (`NASDAQ_BOOK`) and level-one last-trade from a Schwab brokerage stream, recorded by Galyari |
| Session | **One afternoon:** 2026-06-23, 15:07–19:59 ET |
| Stocks | **Three:** ATLN (micro-cap, ~$1.40), NXTS (small-cap, ~$6), GOOG (large-cap, ~$347) |
| Events | **3,045** (ATLN 1,910 · GOOG 901 · NXTS 234) |
| After 16:00 ET | **1,995 of 3,045 (65.5 %)**: ATLN 93.9 %, GOOG 22.3 %, NXTS 0 % |
| Excluded | 47 further events in the store never reach a tested cell: 33 from the simulator, 14 from a fourth stock (SNDK) with too few events |

The three stocks span three tick-size regimes: a $0.01 tick is about 72 bps of price on ATLN,
17 bps on NXTS and 0.3 bps on GOOG. Each stock is observed in a single window, so regime and
stock can't be separated. The micro-cap regime is almost entirely after-hours trading.

## Method

- **Signals.** 18 event types: pull, iceberg and wall resolution (pull / eaten / refresh), each
  on bid and ask; plus four level-state transitions (absorbing, broken, vacuum, watching), each
  at support and resistance. A further 16 conditional sub-signals split pulls and wall pulls by
  context (runway regime, co-located level state).
- **Outcome.** Mid-to-mid return in bps from the event instant at 30 s, 120 s and 300 s. The
  forward mid is the first book frame at or after the horizon, accepted up to one further
  horizon later. A "300 s" return can therefore span 300–600 s.
- **Test cell.** One (signal, stock, horizon) with at least 25 events.
- **Null.** For each cell, 10,000 samples of the same size drawn with replacement from all of
  that stock's events at that horizon. The two-sided p-value compares the cell's deviation from
  the stock's mean to the null's deviation. This is a resampling (bootstrap-style) null
  relative to *other events*, not relative to random times. Fixed seed; the run reproduces
  exactly.
- **Gate.** A signal counts as having an edge only if, at a single horizon, at least two stocks
  have testable cells, all of them agree in sign, and at least one is significant after
  correction.

## Multiple-comparison correction

**138 = 82 unconditional cells + 56 conditional cells.** Each cell is one (signal, stock, horizon)
reaching n ≥ 25. All 138 are treated as one family. Significance is **Holm-adjusted p < 0.05**,
which controls the family-wise error rate. Benjamini–Hochberg q is computed for reference but
doesn't decide verdicts.

The resample count was raised from 2,000 to 10,000 so the correction can be passed at all. With
2,000 resamples the smallest attainable p is 1/2,001 ≈ 0.00050. That is above the Bonferroni
threshold 0.05/138 ≈ 0.00036, so no cell could have survived. With 10,000 the floor is ≈ 0.00010.

| | cells |
|---|---:|
| Tested | 138 |
| Raw p < 0.05 | 23 (≈ 6.9 expected by chance) |
| BH q < 0.05 (reference) | 12 |
| **Holm-adjusted p < 0.05** | **2** |

The two surviving cells:
- level→vacuum (support), ATLN, 300 s: n = 154, Δ = −56.6 bps, Holm p = 0.014.
  Its GOOG counterpart has the opposite sign.
- pull (ask), GOOG, 120 s: n = 131, Δ = −4.4 bps, Holm p = 0.027.
  Its ATLN counterpart has the opposite sign.

Both fail the sign-agreement gate. **0 of 18 signals and 0 of 16 conditional sub-signals pass.**

An earlier run used 2,000 resamples and corrected only across the three horizons. It labeled
two signals as having an edge: level→vacuum (resistance) and pull (bid). Neither survives the
138-cell correction (best Holm p 0.134 and 0.108).

## Results

Δ = cell mean minus the stock's mean over all events, in bps. "Best cell" is the cell with the
smallest raw p.

| signal | cells | stocks | raw p < .05 | min raw p | min Holm p | best cell | verdict |
|---|---:|---|---:|---:|---:|---|---|
| iceberg (ask) | 3 | ATLN | 0 | 0.314 | 1.000 | ATLN 30 s, Δ +11.3, n 49 | no edge |
| iceberg (bid) | 3 | ATLN | 0 | 0.111 | 1.000 | ATLN 30 s, Δ −15.4, n 66 | no edge |
| level→absorbing (resistance) | 3 | ATLN | 2 | 0.0034 | 0.435 | ATLN 300 s, Δ +47.1, n 145 | no edge |
| level→absorbing (support) | 3 | ATLN | 0 | 0.185 | 1.000 | ATLN 300 s, Δ −20.5, n 152 | no edge |
| level→broken (resistance) | 3 | GOOG | 1 | 0.0145 | 1.000 | GOOG 120 s, Δ +5.2, n 29 | no edge |
| level→broken (support) | 3 | GOOG | 1 | 0.0348 | 1.000 | GOOG 120 s, Δ +4.6, n 29 | no edge |
| level→vacuum (resistance) | 9 | ATLN, GOOG, NXTS | 3 | 0.0010 | 0.134 | GOOG 300 s, Δ +4.2, n 99 | no edge |
| level→vacuum (support) | 6 | ATLN, GOOG | 4 | 0.0001 | **0.014** | ATLN 300 s, Δ −56.6, n 154 | no edge (sign flips) |
| level→watching (resistance) | 8 | ATLN, GOOG, NXTS | 3 | 0.0012 | 0.160 | GOOG 120 s, Δ +4.9, n 62 | no edge |
| level→watching (support) | 6 | ATLN, GOOG | 0 | 0.464 | 1.000 | GOOG 30 s, Δ +0.6, n 104 | no edge |
| pull (ask) | 6 | ATLN, GOOG | 3 | 0.0002 | **0.027** | GOOG 120 s, Δ −4.4, n 131 | no edge (sign flips) |
| pull (bid) | 6 | ATLN, GOOG | 1 | 0.0008 | 0.108 | GOOG 120 s, Δ −4.8, n 67 | no edge |
| wall:eaten (ask) | 3 | ATLN | 0 | 0.071 | 1.000 | ATLN 300 s, Δ +59.4, n 33 | no edge |
| wall:eaten (bid) | 3 | ATLN | 0 | 0.161 | 1.000 | ATLN 30 s, Δ −20.0, n 30 | no edge |
| wall:pull (ask) | 6 | ATLN, GOOG | 0 | 0.087 | 1.000 | GOOG 120 s, Δ −2.1, n 91 | no edge |
| wall:pull (bid) | 6 | ATLN, GOOG | 1 | 0.0019 | 0.249 | GOOG 120 s, Δ −4.8, n 53 | no edge |
| wall:refresh (ask) | 3 | ATLN | 0 | 0.162 | 1.000 | ATLN 120 s, Δ +38.3, n 28 | no edge |
| wall:refresh (bid) | 2 | ATLN | 0 | 0.100 | 1.000 | ATLN 120 s, Δ −46.9, n 26 | no edge |

## Limits the correction does not fix

- **Events are not independent.** Forward windows overlap, and level-state events repeat at the
  same price. Effective sample size is much smaller than n. Counting non-overlapping forward
  windows (greedy, from the first event):
  - level→vacuum (resistance), GOOG, 300 s: 6 windows from n = 99
  - pull (ask), GOOG, 120 s: 9 from 131
  - pull (bid), GOOG, 120 s: 11 from 67
  - level→vacuum (support), ATLN, 300 s: 25 from 154

  The null treats every event as an independent draw, so its p-values are too small. The
  correction can't remove this.
- **One afternoon, three stocks, mostly after-hours.** There was no second session, no
  out-of-sample check and no walk-forward.
- **Mid-price returns.** No spread, fees or slippage. The GOOG effects are 2–5 bps.
- **Soft inputs.** On these tapes 31–59 % of derived trades have an ambiguous aggressor side,
  and the book is sampled at a ~1–1.5 s median interval. Detectors can't see anything faster.
