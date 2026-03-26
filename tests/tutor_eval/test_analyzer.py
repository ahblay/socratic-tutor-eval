"""
tests/tutor_eval/test_analyzer.py

Unit tests for pure-Python functions in analyzer.py and metrics.py.
No API calls made.
"""

import pytest
from tutor_eval.evaluation.analyzer import TurnResult, _detect_stalls, _compute_kc_status
from tutor_eval.evaluation.metrics import (
    compute_composite,
    compute_kft,
    compute_lcq,
    compute_mrq,
    compute_nac,
    compute_pr,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _turns(kcs, ps, *, preceding_obs=None, nac=None, kc_status=None,
           observed=None, warranted=None, mrq=None, stall=None):
    """
    Build a list of TurnResult quickly from parallel KC/p lists.

    kcs  — list of kc_id strings (or None for off-map turns)
    ps   — list of p_mastered floats matching kcs
    All keyword args are either a single value (applied to all turns) or a
    list of per-turn values.
    """
    n = len(kcs)

    def _val(v, i):
        return v[i] if isinstance(v, list) else v

    result = []
    for i, (kc, p) in enumerate(zip(kcs, ps)):
        snap = {kc: p} if kc is not None else {}
        obs = _val(preceding_obs, i) if preceding_obs is not None else []
        result.append(TurnResult(
            turn_number=i + 1,
            targeted_kc_id=kc,
            bkt_snapshot=snap,
            preceding_observations=obs,
            nac_verdict=_val(nac, i) if nac is not None else None,
            kc_status=_val(kc_status, i) if kc_status is not None else None,
            observed_type=_val(observed, i) if observed is not None else None,
            warranted_type=_val(warranted, i) if warranted is not None else None,
            mrq_verdict=_val(mrq, i) if mrq is not None else None,
            is_stall_turn=_val(stall, i) if stall is not None else False,
        ))
    return result


def _misconception_obs(kc_id="kc-x"):
    return [{"observation_class": "misconception", "kc_id": kc_id, "evidence_quote": ""}]


# ---------------------------------------------------------------------------
# _detect_stalls
# ---------------------------------------------------------------------------

class TestDetectStalls:

    # ---- Shape 1 (mastered KC drilled) ----

    def test_shape1_marks_at_kth_turn(self):
        """First turn of run counts; K=3 → mark at T3."""
        turns = _turns(["x", "x", "x", "x"], [0.75, 0.76, 0.77, 0.78])
        _detect_stalls(turns, K=3)
        assert not turns[0].is_stall_turn
        assert not turns[1].is_stall_turn
        assert turns[2].is_stall_turn and turns[2].stall_shape == "shape1"
        assert turns[3].is_stall_turn and turns[3].stall_shape == "shape1"

    def test_shape1_just_below_k_no_mark(self):
        turns = _turns(["x", "x"], [0.75, 0.76])
        _detect_stalls(turns, K=3)
        assert not any(t.is_stall_turn for t in turns)

    def test_shape1_exactly_k_marks_only_last(self):
        turns = _turns(["x", "x", "x"], [0.72, 0.73, 0.74])
        _detect_stalls(turns, K=3)
        assert not turns[0].is_stall_turn
        assert not turns[1].is_stall_turn
        assert turns[2].is_stall_turn

    # ---- Shape 2 (frontier stall — no BKT progress) ----

    def test_shape2_marks_at_k_plus_one_turn(self):
        """First turn is baseline (no delta); K=3 delta steps → mark at T4."""
        turns = _turns(["x"] * 5, [0.30, 0.32, 0.33, 0.34, 0.35])
        _detect_stalls(turns, K=3, delta=0.05)
        assert not turns[0].is_stall_turn  # baseline
        assert not turns[1].is_stall_turn  # step 1
        assert not turns[2].is_stall_turn  # step 2
        assert turns[3].is_stall_turn and turns[3].stall_shape == "shape2"
        assert turns[4].is_stall_turn

    def test_shape2_no_stall_when_progress_sufficient(self):
        """Delta >= threshold counts as progress; run resets."""
        turns = _turns(["x"] * 4, [0.30, 0.36, 0.42, 0.48])  # each delta = 0.06 ≥ 0.05
        _detect_stalls(turns, K=3, delta=0.05)
        assert not any(t.is_stall_turn for t in turns)

    def test_shape2_first_turn_not_counted_in_run(self):
        """T1 is baseline; stall requires K more turns after it."""
        turns = _turns(["x"] * 3, [0.30, 0.31, 0.32])  # only 2 delta steps after T1
        _detect_stalls(turns, K=3, delta=0.05)
        assert not any(t.is_stall_turn for t in turns)

    # ---- Shape transition mid-run ----

    def test_shape1_to_shape2_transition(self):
        """T1 p≥0.7 (Shape 1, count=1), T2 p drops to 0.65 (delta<δ → Shape 2, count=2),
        T3 continues Shape 2 (count=3) → mark T3."""
        turns = _turns(["x", "x", "x"], [0.75, 0.65, 0.63])
        _detect_stalls(turns, K=3, delta=0.05)
        assert not turns[0].is_stall_turn
        assert not turns[1].is_stall_turn
        assert turns[2].is_stall_turn and turns[2].stall_shape == "shape2"

    # ---- Progress reset ----

    def test_progress_mid_run_resets_counter(self):
        """Sufficient delta on T3 resets; new stall builds from T3 onward."""
        # T1 baseline, T2 stall step (1), T3 progress (delta=0.10 reset),
        # T4 step 1, T5 step 2, T6 step 3 → mark T6
        turns = _turns(["x"] * 6, [0.30, 0.32, 0.42, 0.44, 0.45, 0.46])
        _detect_stalls(turns, K=3, delta=0.05)
        assert not turns[0].is_stall_turn
        assert not turns[1].is_stall_turn
        assert not turns[2].is_stall_turn  # progress, reset
        assert not turns[3].is_stall_turn
        assert not turns[4].is_stall_turn
        assert turns[5].is_stall_turn

    # ---- KC change ----

    def test_kc_change_resets_counter(self):
        """Switching KC resets the stall run."""
        turns = _turns(
            ["x", "x", "y", "y", "y"],
            [0.75, 0.76, 0.75, 0.76, 0.77],
        )
        _detect_stalls(turns, K=3)
        # KC=x run: T1 (count=1), T2 (count=2) — only 2 turns, no mark
        # KC=y run: T3 (count=1), T4 (count=2), T5 (count=3) → mark T5
        assert not turns[0].is_stall_turn
        assert not turns[1].is_stall_turn
        assert not turns[2].is_stall_turn
        assert not turns[3].is_stall_turn
        assert turns[4].is_stall_turn

    # ---- Misconception exemption ----

    def test_misconception_resets_counter(self):
        """Misconception in preceding_observations resets stall counter."""
        obs = [_misconception_obs(), [], [], [], []]
        turns = _turns(
            ["x"] * 5,
            [0.75, 0.76, 0.77, 0.78, 0.79],
            preceding_obs=obs,
        )
        # T1 has misconception → reset; T2 count=1, T3 count=2, T4 count=3 → mark T4
        _detect_stalls(turns, K=3)
        assert not turns[0].is_stall_turn  # misconception reset, not counted
        assert not turns[1].is_stall_turn
        assert not turns[2].is_stall_turn
        assert turns[3].is_stall_turn

    def test_misconception_mid_stall_run_resets(self):
        """Misconception at turn 3 (K=3 run nearly complete) resets counter."""
        obs = [[], [], _misconception_obs(), [], [], []]
        turns = _turns(
            ["x"] * 6,
            [0.75, 0.76, 0.77, 0.78, 0.79, 0.80],
            preceding_obs=obs,
        )
        _detect_stalls(turns, K=3)
        # T1 count=1, T2 count=2, T3 misconception → reset
        # T4 count=1, T5 count=2, T6 count=3 → mark T6
        assert not turns[2].is_stall_turn
        assert not turns[3].is_stall_turn
        assert not turns[4].is_stall_turn
        assert turns[5].is_stall_turn

    # ---- None KC handling ----

    def test_none_kc_resets_run(self):
        """A turn with targeted_kc_id=None breaks any stall run."""
        turns = _turns(
            ["x", "x", None, "x", "x", "x"],
            [0.75, 0.76, 0.0,  0.75, 0.76, 0.77],
        )
        _detect_stalls(turns, K=3)
        # First run: T1 count=1, T2 count=2 — cut by None at T3
        # New run after T4: T4 count=1, T5 count=2, T6 count=3 → mark T6
        assert not turns[0].is_stall_turn
        assert not turns[1].is_stall_turn
        assert not turns[2].is_stall_turn
        assert not turns[3].is_stall_turn
        assert not turns[4].is_stall_turn
        assert turns[5].is_stall_turn

    def test_no_stall_empty_turns(self):
        _detect_stalls([], K=3)  # should not raise


# ---------------------------------------------------------------------------
# _compute_kc_status
# ---------------------------------------------------------------------------

class TestComputeKcStatus:
    KG = {
        "kcs": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}],
    }

    def test_on_frontier(self):
        assert _compute_kc_status("b", {"a": 0.9, "b": 0.2}, ["b"], self.KG) == "on_frontier"

    def test_mastered(self):
        assert _compute_kc_status("a", {"a": 0.8}, ["b"], self.KG) == "mastered"

    def test_prereqs_not_met(self):
        # c is not on frontier because b is not mastered
        assert _compute_kc_status("c", {"a": 0.9, "b": 0.2, "c": 0.1}, ["b"], self.KG) == "prereqs_not_met"

    def test_off_map_unknown_kc(self):
        assert _compute_kc_status("z", {"a": 0.9}, [], self.KG) == "off_map"

    def test_off_map_none(self):
        assert _compute_kc_status(None, {}, [], self.KG) == "off_map"


# ---------------------------------------------------------------------------
# compute_nac
# ---------------------------------------------------------------------------

class TestComputeNAC:
    def test_all_compliant(self):
        turns = _turns(["x"] * 3, [0.5] * 3, nac="compliant")
        assert compute_nac(turns) == 1.0

    def test_all_violation(self):
        turns = _turns(["x"] * 3, [0.5] * 3, nac="violation")
        assert compute_nac(turns) == 0.0

    def test_all_disabled_returns_one(self):
        turns = _turns(["x"] * 4, [0.5] * 4, nac="disabled")
        assert compute_nac(turns) == 1.0

    def test_mixed_excludes_disabled(self):
        # 2 compliant, 1 violation, 1 disabled → 2/3
        turns = _turns(
            ["x"] * 4, [0.5] * 4,
            nac=["compliant", "compliant", "violation", "disabled"],
        )
        assert abs(compute_nac(turns) - 2 / 3) < 1e-9

    def test_empty_returns_one(self):
        assert compute_nac([]) == 1.0


# ---------------------------------------------------------------------------
# compute_kft
# ---------------------------------------------------------------------------

class TestComputeKFT:
    def test_all_on_frontier(self):
        turns = _turns(["x"] * 4, [0.5] * 4, kc_status="on_frontier")
        assert compute_kft(turns) == 1.0

    def test_all_off_map(self):
        turns = _turns(["x"] * 3, [0.5] * 3, kc_status="off_map")
        assert compute_kft(turns) == 0.0

    def test_none_status_counts_as_off_map(self):
        # Turns with kc_status=None are included in denominator as 0-score turns
        turns = _turns(["x"] * 4, [0.5] * 4,
                       kc_status=["on_frontier", None, None, None])
        assert compute_kft(turns) == 0.25

    def test_mixed(self):
        statuses = ["on_frontier", "mastered", "on_frontier", "prereqs_not_met"]
        turns = _turns(["x"] * 4, [0.5] * 4, kc_status=statuses)
        assert compute_kft(turns) == 0.5

    def test_empty(self):
        assert compute_kft([]) == 0.0


# ---------------------------------------------------------------------------
# compute_pr
# ---------------------------------------------------------------------------

class TestComputePR:
    def test_no_stalls(self):
        turns = _turns(["x"] * 5, [0.5] * 5, stall=False)
        assert compute_pr(turns) == 1.0

    def test_all_stalls(self):
        turns = _turns(["x"] * 4, [0.5] * 4, stall=True)
        assert compute_pr(turns) == 0.0

    def test_partial_stalls(self):
        stalls = [False, False, True, True]
        turns = _turns(["x"] * 4, [0.5] * 4, stall=stalls)
        assert compute_pr(turns) == 0.5

    def test_empty(self):
        assert compute_pr([]) == 1.0

    def test_pr_uses_is_stall_turn_field_directly(self):
        """PR reads TurnResult.is_stall_turn — not re-running stall detection."""
        turns = _turns(["x"] * 3, [0.5] * 3, stall=[True, False, True])
        assert abs(compute_pr(turns) - 1 / 3) < 1e-9


# ---------------------------------------------------------------------------
# compute_lcq
# ---------------------------------------------------------------------------

class TestComputeLCQ:
    def test_all_aligned(self):
        turns = _turns(["x"] * 3, [0.5] * 3, observed="concept", warranted="concept")
        assert compute_lcq(turns) == 1.0

    def test_none_aligned(self):
        turns = _turns(["x"] * 3, [0.5] * 3, observed="concept", warranted="narrative")
        assert compute_lcq(turns) == 0.0

    def test_none_classifications_excluded(self):
        # 2 aligned, 1 misaligned, 1 both-None (excluded from denominator)
        turns = _turns(
            ["x"] * 4, [0.5] * 4,
            observed=["concept", "concept", "narrative", None],
            warranted=["concept", "concept", "concept", None],
        )
        assert abs(compute_lcq(turns) - 2 / 3) < 1e-9

    def test_partial_none_excluded(self):
        # observed=None but warranted set → excluded
        turns = _turns(
            ["x"] * 3, [0.5] * 3,
            observed=[None, "concept", "narrative"],
            warranted=["concept", "concept", "concept"],
        )
        assert abs(compute_lcq(turns) - 1 / 2) < 1e-9

    def test_all_none_returns_zero(self):
        turns = _turns(["x"] * 3, [0.5] * 3)  # observed/warranted default to None
        assert compute_lcq(turns) == 0.0

    def test_empty(self):
        assert compute_lcq([]) == 0.0


# ---------------------------------------------------------------------------
# compute_mrq
# ---------------------------------------------------------------------------

class TestComputeMRQ:
    def test_no_misconception_turns_returns_none(self):
        turns = _turns(["x"] * 4, [0.5] * 4)  # mrq_verdict=None
        assert compute_mrq(turns) is None

    def test_all_probed(self):
        turns = _turns(["x"] * 3, [0.5] * 3, mrq="probed")
        assert compute_mrq(turns) == 1.0

    def test_all_ignored(self):
        turns = _turns(["x"] * 3, [0.5] * 3, mrq="ignored")
        assert compute_mrq(turns) == 0.0

    def test_mixed(self):
        turns = _turns(
            ["x"] * 4, [0.5] * 4,
            mrq=["probed", "ignored", "probed", None],
        )
        # None excluded; 2 probed / 3 misconception turns
        assert abs(compute_mrq(turns) - 2 / 3) < 1e-9

    def test_none_mrq_turns_excluded(self):
        """Turns with mrq_verdict=None (no misconception) don't count."""
        turns = _turns(["x"] * 3, [0.5] * 3, mrq=[None, None, "probed"])
        assert compute_mrq(turns) == 1.0

    def test_empty(self):
        assert compute_mrq([]) is None


# ---------------------------------------------------------------------------
# compute_composite
# ---------------------------------------------------------------------------

class TestComputeComposite:
    def test_formula(self):
        # NAC=1, KFT=1, PR=1, LCQ=1, adj=0 → 1*(0.5+0.25+0.25+0) = 1.0
        assert abs(compute_composite(1.0, 1.0, 1.0, 1.0, 0.0) - 1.0) < 1e-9

    def test_nac_zero_collapses_score(self):
        assert compute_composite(0.0, 1.0, 1.0, 1.0, 0.0) == 0.0

    def test_positive_mrq_adjustment_can_exceed_one(self):
        # NAC=1, KFT=1, PR=1, LCQ=1, MRQ=1.0 → adj=0.15*(1-0.5)=0.075
        score = compute_composite(1.0, 1.0, 1.0, 1.0, 0.075)
        assert score > 1.0
        assert abs(score - 1.075) < 1e-9

    def test_negative_mrq_adjustment_penalizes(self):
        # MRQ=0.0 → adj=0.15*(0-0.5)=-0.075
        score = compute_composite(1.0, 1.0, 1.0, 1.0, -0.075)
        assert abs(score - 0.925) < 1e-9

    def test_zero_everything(self):
        assert compute_composite(0.0, 0.0, 0.0, 0.0, 0.0) == 0.0

    def test_weights_sum_to_one_without_adjustment(self):
        # With all component scores = 1 and no adjustment, result = NAC
        assert abs(compute_composite(0.8, 1.0, 1.0, 1.0, 0.0) - 0.8) < 1e-9

    def test_kft_weight_double_pr_lcq(self):
        # KFT=1, PR=0, LCQ=0, adj=0 → 0.5; PR=1, KFT=0, LCQ=0 → 0.25
        kft_only = compute_composite(1.0, 1.0, 0.0, 0.0, 0.0)
        pr_only  = compute_composite(1.0, 0.0, 1.0, 0.0, 0.0)
        assert abs(kft_only - 0.5) < 1e-9
        assert abs(pr_only  - 0.25) < 1e-9
        assert abs(kft_only - 2 * pr_only) < 1e-9
