"""
tests/convolearn/test_parse.py

Unit tests for convolearn/parse.py.
No real API or HuggingFace calls — load_dataset is mocked throughout.
Covers:
  - _extract_first_student
  - _derive_slug (parse variant — 80-char limit)
  - load_and_sample (filtering, sampling, output format)
"""

import pytest
from unittest.mock import patch

from convolearn.parse import _extract_first_student, _derive_slug, _count_tutor_turns, load_and_sample


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conv(student_q: str, n_exchanges: int = 12) -> str:
    """Conversation with an opening Student turn + n_exchanges (Teacher+Student) pairs.

    n_exchanges equals the number of teacher turns (= total_tutor_turns in output).
    Default n_exchanges=12 → 12 teacher turns, safely above the min_messages=10 threshold.
    Use n_exchanges=10 to create a dialogue that falls below the threshold.
    Use n_exchanges=11 for exactly at the threshold.
    """
    lines = [f"Student: {student_q}"]
    for i in range(n_exchanges):
        lines.append(f"Teacher: Response {i}.")
        lines.append("Student: Follow-up.")
    return "\n".join(lines)


def _make_rows(student_q: str, count: int, n_exchanges: int = 12, num_exchanges: int = 11) -> list[dict]:
    """Return `count` fake dataset rows for a given opening question.

    n_exchanges controls the actual conversation length (post-normalization turn count).
    num_exchanges is the metadata field stored in the row (not used for filtering).
    """
    return [
        {
            "cleaned_conversation": _conv(student_q, n_exchanges),
            "earthscience_topic": "Earth's Energy",
            "num_exchanges": num_exchanges,
            "effectiveness_consensus": 3.0 + (i % 2) * 0.5,
            "completeness_consensus": 2.0,
        }
        for i in range(count)
    ]


# ===========================================================================
# _count_turns
# ===========================================================================

class TestCountTutorTurns:

    def test_empty_string_returns_zero(self):
        assert _count_tutor_turns("") == 0

    def test_only_student_returns_zero(self):
        assert _count_tutor_turns("Student: Hello?") == 0

    def test_single_teacher_turn(self):
        assert _count_tutor_turns("Teacher: Think about it.") == 1

    def test_counts_only_teacher_turns(self):
        conv = "Student: Why?\nTeacher: Think.\nStudent: I see.\nTeacher: Good."
        assert _count_tutor_turns(conv) == 2  # 4 total turns but only 2 teacher

    def test_multiline_teacher_counts_as_one_turn(self):
        conv = (
            "Student: Why?\n"
            "Teacher: First line.\n"
            "Continuation of teacher response.\n"
            "Student: I see.\n"
        )
        assert _count_tutor_turns(conv) == 1

    def test_empty_teacher_label_not_counted(self):
        conv = "Teacher:\nStudent: Hello."
        assert _count_tutor_turns(conv) == 0  # empty teacher content excluded

    def test_conv_helper_n_exchanges_equals_teacher_turns(self):
        # n_exchanges=10 → exactly 10 teacher turns
        assert _count_tutor_turns(_conv("Why?", n_exchanges=10)) == 10

    def test_conv_helper_default_produces_above_threshold(self):
        # default n_exchanges=12 → 12 teacher turns > 10
        assert _count_tutor_turns(_conv("Why?")) == 12


# ===========================================================================
# _extract_first_student
# ===========================================================================

class TestExtractFirstStudent:

    def test_returns_first_student_line(self):
        conv = "Student: Why is the sky blue?\nTeacher: Think about it."
        assert _extract_first_student(conv) == "Why is the sky blue?"

    def test_teacher_first_then_student(self):
        conv = "Teacher: Hello.\nStudent: What is evaporation?"
        assert _extract_first_student(conv) == "What is evaporation?"

    def test_returns_only_first_student_line(self):
        conv = "Student: First question.\nStudent: Second question."
        assert _extract_first_student(conv) == "First question."

    def test_no_student_line_returns_empty(self):
        conv = "Teacher: Hello.\nTeacher: Anything?"
        assert _extract_first_student(conv) == ""

    def test_empty_string_returns_empty(self):
        assert _extract_first_student("") == ""

    def test_strips_leading_and_trailing_whitespace(self):
        conv = "Student:   padded question   \nTeacher: yes."
        result = _extract_first_student(conv)
        assert result == "padded question"

    def test_multiline_conversation_correct_extraction(self):
        conv = (
            "Student: Why does the equator receive more solar energy?\n"
            "Teacher: What shape is the Earth?\n"
            "Student: Roughly spherical.\n"
        )
        assert _extract_first_student(conv) == "Why does the equator receive more solar energy?"

    def test_colon_in_content_not_truncated(self):
        conv = "Student: What is H2O: water or oxygen?\nTeacher: Good question."
        assert _extract_first_student(conv) == "What is H2O: water or oxygen?"


# ===========================================================================
# _derive_slug (parse.py variant — 80-char limit)
# ===========================================================================

class TestDeriveSlugParse:

    def test_spaces_become_hyphens(self):
        assert _derive_slug("ocean currents") == "ocean-currents"

    def test_uppercase_lowercased(self):
        assert _derive_slug("SOLAR ENERGY") == "solar-energy"

    def test_special_chars_stripped(self):
        assert _derive_slug("What's happening?") == "what-s-happening"

    def test_truncated_at_80_not_64(self):
        # 100 chars → 80
        slug = _derive_slug("a" * 100)
        assert len(slug) == 80

    def test_exactly_80_chars_not_truncated(self):
        slug = _derive_slug("a" * 80)
        assert len(slug) == 80

    def test_no_leading_or_trailing_hyphens(self):
        slug = _derive_slug("  --hello--  ")
        assert not slug.startswith("-")
        assert not slug.endswith("-")

    def test_numbers_preserved(self):
        assert _derive_slug("CO2 levels") == "co2-levels"


# ===========================================================================
# load_and_sample
# ===========================================================================

class TestLoadAndSample:

    def _patch(self, rows):
        """Return a context manager that patches load_dataset to yield `rows`."""
        return patch("datasets.load_dataset", return_value=rows)

    # --- Filtering: min_messages (post-normalization turn count) ---

    def test_rows_below_min_messages_filtered_out(self):
        # n_exchanges=12 → 12 teacher turns (qualifies); n_exchanges=8 → 8 teacher turns (below)
        rows = (
            _make_rows("Sky blue?", count=25, n_exchanges=12)
            + _make_rows("Sky blue?", count=5,  n_exchanges=8)
        )
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, min_messages=10, sample_size=5)
        # 25 qualifying rows ≥ 20 → prompt kept
        assert len(result) == 1
        assert len(result[0]["dialogues"]) == 25

    def test_all_rows_below_min_messages_excludes_prompt(self):
        # n_exchanges=7 → 7 teacher turns, well below 10
        rows = _make_rows("Sky blue?", count=25, n_exchanges=7)
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, min_messages=10, sample_size=5)
        assert result == []

    def test_exactly_min_messages_qualifies(self):
        # n_exchanges=10 → exactly 10 teacher turns, at threshold
        rows = _make_rows("Sky blue?", count=20, n_exchanges=10)
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, min_messages=10, sample_size=5)
        assert len(result) == 1

    def test_one_below_min_messages_excluded(self):
        # n_exchanges=9 → 9 teacher turns, just below threshold of 10
        rows = _make_rows("Sky blue?", count=25, n_exchanges=9)
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, min_messages=10, sample_size=5)
        assert result == []

    # --- Filtering: min_dialogues ---

    def test_prompt_with_too_few_qualifying_rows_excluded(self):
        # "Sky blue?" has 25 qualifying rows; "Rain?" has only 10
        rows = (
            _make_rows("Sky blue?", count=25)
            + _make_rows("Rain?",    count=10)
        )
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, min_messages=10, sample_size=5)
        assert len(result) == 1
        assert result[0]["question_prompt"] == "Sky blue?"

    def test_exactly_min_dialogues_included(self):
        rows = _make_rows("Sky blue?", count=20)
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, min_messages=10, sample_size=5)
        assert len(result) == 1

    # --- Sampling ---

    def test_samples_up_to_sample_size(self):
        # 5 distinct qualifying prompts, sample_size=3
        rows = []
        for i in range(5):
            rows += _make_rows(f"Question {i}?", count=25)
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, min_messages=10, sample_size=3)
        assert len(result) == 3

    def test_fewer_qualifying_prompts_than_sample_size_returns_all(self):
        rows = _make_rows("Only prompt?", count=25)
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, min_messages=10, sample_size=10)
        assert len(result) == 1

    def test_empty_dataset_returns_empty(self):
        with self._patch([]):
            result = load_and_sample()
        assert result == []

    def test_seed_produces_reproducible_results(self):
        rows = []
        for i in range(10):
            rows += _make_rows(f"Question {i}?", count=25)
        with self._patch(rows):
            r1 = load_and_sample(sample_size=5, seed=42)
        with self._patch(rows):
            r2 = load_and_sample(sample_size=5, seed=42)
        assert [r["question_prompt"] for r in r1] == [r["question_prompt"] for r in r2]

    def test_different_seeds_may_produce_different_results(self):
        rows = []
        for i in range(20):
            rows += _make_rows(f"Question {i}?", count=25)
        with self._patch(rows):
            r1 = load_and_sample(sample_size=5, seed=1)
        with self._patch(rows):
            r2 = load_and_sample(sample_size=5, seed=99)
        # With 20 qualifying prompts sampled to 5, seeds 1 and 99 very likely differ
        assert [r["question_prompt"] for r in r1] != [r["question_prompt"] for r in r2]

    # --- Output format ---

    def test_output_has_required_keys(self):
        rows = _make_rows("Sky blue?", count=20)
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, sample_size=5)
        entry = result[0]
        assert "prompt_id" in entry
        assert "question_prompt" in entry
        assert "earthscience_topic" in entry
        assert "dialogues" in entry

    def test_prompt_id_is_slug_of_question(self):
        rows = _make_rows("Sky blue?", count=20)
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, sample_size=5)
        assert result[0]["prompt_id"] == "sky-blue"

    def test_dialogue_idx_sequential_from_zero(self):
        rows = _make_rows("Sky blue?", count=20)
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, sample_size=5)
        idxs = [d["dialogue_idx"] for d in result[0]["dialogues"]]
        assert idxs == list(range(20))

    def test_dialogue_contains_ground_truth_fields(self):
        rows = _make_rows("Sky blue?", count=20)
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, sample_size=5)
        d = result[0]["dialogues"][0]
        assert "effectiveness_consensus" in d
        assert "completeness_consensus" in d
        assert "num_exchanges" in d
        assert "cleaned_conversation" in d

    def test_earthscience_topic_from_first_row(self):
        rows = _make_rows("Sky blue?", count=20)
        rows[0]["earthscience_topic"] = "Special Topic"
        with self._patch(rows):
            result = load_and_sample(min_dialogues=20, sample_size=5)
        assert result[0]["earthscience_topic"] == "Special Topic"

    def test_rows_with_no_student_line_are_skipped(self):
        no_student = [
            {
                "cleaned_conversation": "Teacher: Hello.\nTeacher: Yes.",
                "earthscience_topic": "Light",
                "num_exchanges": 11,
                "effectiveness_consensus": 3.0,
                "completeness_consensus": 2.0,
            }
        ] * 25
        with self._patch(no_student):
            result = load_and_sample(min_dialogues=20, sample_size=5)
        assert result == []
