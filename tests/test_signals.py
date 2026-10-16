"""Tests for the E3 conditional-edge gate (signals.py)."""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals import permutation_p, verdict, signal_name, measure, cond_namer, ALPHA


def test_permutation_p_detects_real_shift():
    rng = random.Random(1)
    pool = [rng.gauss(0, 10) for _ in range(2000)]
    # a group whose mean is far from the pool mean → low p
    assert permutation_p(observed_mean=8.0, pool=pool, n=50, rng=rng) < 0.05
    # a group at the pool mean → high p
    assert permutation_p(observed_mean=0.0, pool=pool, n=50, rng=rng) > 0.2


def _cell(n, mean, base, p):
    return {"n": n, "mean": mean, "base": base, "p": p}


def test_verdict_measured_edge_two_consistent_one_sig():
    cells = {30: {"ATLN": _cell(50, 5, 0, 0.20), "GOOG": _cell(50, 4, 0, 0.01)}}
    label, _ = verdict(cells)
    assert label == "MEASURED-EDGE"


def test_verdict_visual_only_single_name():
    cells = {30: {"GOOG": _cell(50, 4, 0, 0.001)}}
    label, why = verdict(cells)
    assert label == "VISUAL-ONLY" and "single-name" in why


def test_verdict_visual_only_sign_flip():
    # two symbols significant but opposite sign → overfit, not edge (the Phase A lesson)
    cells = {30: {"ATLN": _cell(50, +5, 0, 0.01), "GOOG": _cell(50, -5, 0, 0.01)}}
    label, why = verdict(cells)
    assert label == "VISUAL-ONLY" and "FLIPS" in why


def test_verdict_consistency_and_significance_same_horizon():
    # consistent-but-not-sig at 30s, sig-but-inconsistent at 120s → must NOT pass
    cells = {
        30: {"ATLN": _cell(50, +5, 0, 0.30), "GOOG": _cell(50, +4, 0, 0.30)},
        120: {"ATLN": _cell(50, +5, 0, 0.01), "GOOG": _cell(50, -4, 0, 0.01)},
    }
    label, _ = verdict(cells)
    assert label == "VISUAL-ONLY"


def test_signal_name_formats():
    assert signal_name({"kind": "wall_resolution", "side": "ask",
                        "detail": {"resolution": "pull"}}) == "wall:pull (ask)"
    assert signal_name({"kind": "level_state", "side": "support",
                        "detail": {"new_state": "vacuum"}}) == "level→vacuum (support)"


def test_cond_namer_splits_by_context():
    pull_clear = {"kind": "pull", "side": "ask",
                  "detail": {"ctx_runway_label": "clear", "ctx_level": "vacuum"}}
    pull_blocked = {"kind": "pull", "side": "ask",
                    "detail": {"ctx_runway_label": "blocked", "ctx_level": "none"}}
    level_ev = {"kind": "level_state", "side": "ask", "detail": {"new_state": "vacuum"}}

    rw = cond_namer("runway")
    assert rw(pull_clear) == "pull (ask) @runway:clear"
    assert rw(pull_blocked) == "pull (ask) @runway:blocked"
    assert rw(level_ev) is None                 # conditioning only applies to pull/wall

    lv = cond_namer("level")
    assert lv(pull_clear) == "pull (ask) @lvl:vacuum"
    assert lv(pull_blocked) is None             # 'none' co-located level → excluded


def test_measure_with_cond_namer():
    events = [{"symbol": "T", "kind": "pull", "side": "ask",
               "detail": {"ctx_runway_label": "clear"},
               "labels": {"ret_bps_30s": 10.0}} for _ in range(30)]
    events += [{"symbol": "T", "kind": "pull", "side": "ask",
                "detail": {"ctx_runway_label": "blocked"},
                "labels": {"ret_bps_30s": -10.0}} for _ in range(30)]
    out = measure(events, 30, random.Random(0), namer=cond_namer("runway"))
    assert "pull (ask) @runway:clear" in out and "pull (ask) @runway:blocked" in out


def test_measure_groups_and_filters_by_min_n():
    rng = random.Random(0)
    events = []
    # 40 vacuum-resistance events on T with a +shift, plus filler so the pool exists
    for i in range(40):
        events.append({"symbol": "T", "kind": "level_state", "side": "resistance",
                       "detail": {"new_state": "vacuum"}, "labels": {"ret_bps_30s": 20.0}})
    for i in range(60):
        events.append({"symbol": "T", "kind": "pull", "side": "bid",
                       "detail": {}, "labels": {"ret_bps_30s": rng.gauss(0, 5)}})
    out = measure(events, 30, rng)
    assert "level→vacuum (resistance)" in out
    assert out["level→vacuum (resistance)"]["T"]["n"] == 40


def test_holm_and_bh_known_values():
    from signals import holm, bh
    ps = [0.01, 0.04, 0.03, 0.005]
    # Holm: sorted 0.005*4, 0.01*3, 0.03*2, 0.04*1 → monotone 0.02, 0.03, 0.06, 0.06
    assert [round(x, 6) for x in holm(ps)] == [0.03, 0.06, 0.06, 0.02]
    # BH: 0.04*4/4, 0.03*4/3, 0.01*4/2, 0.005*4/1 → step-up min → 0.04, 0.04, 0.02, 0.02
    assert [round(x, 6) for x in bh(ps)] == [0.02, 0.04, 0.04, 0.02]


def test_verdict_uses_corrected_p():
    # raw p significant, Holm-adjusted not → must not pass when gating on p_holm
    cells = {30: {"ATLN": {**_cell(50, 5, 0, 0.20), "p_holm": 1.0},
                  "GOOG": {**_cell(50, 4, 0, 0.01), "p_holm": 0.5}}}
    assert verdict(cells)[0] == "MEASURED-EDGE"
    assert verdict(cells, pkey="p_holm")[0] == "VISUAL-ONLY"


def test_adjust_family_spans_all_tables():
    from signals import adjust_family
    a = {30: {"s1": {"T": _cell(30, 1, 0, 0.01)}}}
    b = {30: {"s2": {"T": _cell(30, 1, 0, 0.02)}}, 120: {"s2": {"U": _cell(30, 1, 0, 0.5)}}}
    assert adjust_family([a, b]) == 3
    assert round(a[30]["s1"]["T"]["p_holm"], 6) == 0.03
