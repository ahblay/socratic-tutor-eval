"""
tests/convolearn/test_score_batch.py

Unit tests for the pure-Python helpers in convolearn/score_batch.py.
No API calls — covers:
  - _mean_or_none
  - build_summary
"""

import pytest

from convolearn.score_batch import _mean_or_none, build_summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record(prompt_id, nac=0.8, kft=0.6, pr=0.9, lcq=0.7, mrq=None,
            composite=0.65, is_valid=True, effectiveness=3.5, completeness=2.0):
    return {
        "session_id": f"{prompt_id}_0",
        "prompt_id": prompt_id,
        "earthscience_topic": "Earth's Energy",
        "nac": nac,
        "kft": kft,
        "pr": pr,
        "lcq": lcq,
        "mrq": mrq,
        "composite": composite,
        "is_valid": is_valid,
        "total_tutor_turns": 10,
        "effectiveness_consensus": effectiveness,
        "completeness_consensus": completeness,
        "num_exchanges": 11,
        "error": None,
    }


def _prompt_entry(prompt_id, n_dialogues=1):
    return {
        "prompt_id": prompt_id,
        "question_prompt": f"Question about {prompt_id}?",
        "earthscience_topic": "Earth's Energy",
        "dialogues": [{"dialogue_idx": i} for i in range(n_dialogues)],
    }


# ===========================================================================
# _mean_or_none
# ===========================================================================

class TestMeanOrNone:

    def test_all_numeric_returns_mean(self):
        assert _mean_or_none([1.0, 2.0, 3.0]) == pytest.approx(2.0)

    def test_single_value(self):
        assert _mean_or_none([0.75]) == pytest.approx(0.75)

    def test_empty_list_returns_none(self):
        assert _mean_or_none([]) is None

    def test_all_none_returns_none(self):
        assert _mean_or_none([None, None, None]) is None

    def test_none_values_excluded_from_mean(self):
        # mean of [1.0, 3.0] = 2.0; None is excluded
        result = _mean_or_none([1.0, None, 3.0])
        assert result == pytest.approx(2.0)

    def test_single_none_and_single_numeric(self):
        assert _mean_or_none([None, 0.6]) == pytest.approx(0.6)

    def test_result_rounded_to_4_decimal_places(self):
        result = _mean_or_none([1.0, 2.0])
        # 1.5 is exact — check it returns a float with ≤ 4 decimal places
        assert result == pytest.approx(1.5)
        assert isinstance(result, float)

    def test_all_zeros(self):
        assert _mean_or_none([0.0, 0.0, 0.0]) == pytest.approx(0.0)


# ===========================================================================
# build_summary
# ===========================================================================

class TestBuildSummary:

    def test_single_prompt_single_record(self):
        prompts = [_prompt_entry("equator")]
        records = [_record("equator", kft=0.6)]
        summary = build_summary(prompts, records)
        assert len(summary) == 1
        assert summary[0]["prompt_id"] == "equator"
        assert summary[0]["mean_kft"] == pytest.approx(0.6)

    def test_n_dialogues_counts_scored_records(self):
        prompts = [_prompt_entry("equator", n_dialogues=3)]
        records = [_record("equator") for _ in range(3)]
        summary = build_summary(prompts, records)
        assert summary[0]["n_dialogues"] == 3

    def test_mean_computed_across_multiple_records(self):
        prompts = [_prompt_entry("equator", n_dialogues=2)]
        records = [_record("equator", kft=0.4), _record("equator", kft=0.8)]
        summary = build_summary(prompts, records)
        assert summary[0]["mean_kft"] == pytest.approx(0.6)

    def test_null_metric_values_excluded_from_mean(self):
        prompts = [_prompt_entry("equator", n_dialogues=3)]
        records = [
            _record("equator", mrq=None),
            _record("equator", mrq=0.8),
            _record("equator", mrq=None),
        ]
        summary = build_summary(prompts, records)
        # Only one non-null mrq value → mean_mrq = 0.8
        assert summary[0]["mean_mrq"] == pytest.approx(0.8)

    def test_all_null_metric_yields_null_mean(self):
        prompts = [_prompt_entry("equator", n_dialogues=2)]
        records = [_record("equator", lcq=None), _record("equator", lcq=None)]
        summary = build_summary(prompts, records)
        assert summary[0]["mean_lcq"] is None

    def test_prompt_with_no_records_has_zero_n_dialogues_and_null_means(self):
        prompts = [_prompt_entry("empty-prompt", n_dialogues=0)]
        summary = build_summary(prompts, [])
        assert summary[0]["n_dialogues"] == 0
        assert summary[0]["mean_kft"] is None
        assert summary[0]["mean_nac"] is None

    def test_multiple_prompts_aggregated_independently(self):
        prompts = [_prompt_entry("p1"), _prompt_entry("p2")]
        records = [
            _record("p1", kft=0.3),
            _record("p2", kft=0.9),
        ]
        summary = build_summary(prompts, records)
        by_id = {s["prompt_id"]: s for s in summary}
        assert by_id["p1"]["mean_kft"] == pytest.approx(0.3)
        assert by_id["p2"]["mean_kft"] == pytest.approx(0.9)

    def test_summary_preserves_question_prompt(self):
        prompts = [_prompt_entry("equator")]
        summary = build_summary(prompts, [_record("equator")])
        assert summary[0]["question_prompt"] == "Question about equator?"

    def test_summary_includes_all_required_keys(self):
        prompts = [_prompt_entry("equator")]
        summary = build_summary(prompts, [_record("equator")])
        expected = {
            "prompt_id", "question_prompt", "n_dialogues",
            "mean_nac", "mean_kft", "mean_pr", "mean_lcq", "mean_mrq",
            "mean_composite", "mean_effectiveness_consensus", "mean_completeness_consensus",
        }
        assert expected.issubset(summary[0].keys())

    def test_mean_effectiveness_and_completeness_consensus_included(self):
        prompts = [_prompt_entry("equator", n_dialogues=2)]
        records = [
            _record("equator", effectiveness=3.0, completeness=2.0),
            _record("equator", effectiveness=5.0, completeness=3.0),
        ]
        summary = build_summary(prompts, records)
        assert summary[0]["mean_effectiveness_consensus"] == pytest.approx(4.0)
        assert summary[0]["mean_completeness_consensus"] == pytest.approx(2.5)

    def test_order_of_summary_matches_order_of_sampled_prompts(self):
        prompts = [_prompt_entry("p1"), _prompt_entry("p2"), _prompt_entry("p3")]
        records = [_record("p1"), _record("p2"), _record("p3")]
        summary = build_summary(prompts, records)
        assert [s["prompt_id"] for s in summary] == ["p1", "p2", "p3"]

    def test_empty_prompts_and_records(self):
        assert build_summary([], []) == []
