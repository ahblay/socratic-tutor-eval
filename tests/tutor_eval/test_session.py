"""
tests/tutor_eval/test_session.py

Unit tests for tutor_eval/session.py.
Uses stub tutor and student objects — no API calls.
"""

import pytest
from tutor_eval.session import run_session
from tutor_eval.tutors.base import AbstractTutor


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _FixedTutor(AbstractTutor):
    """Cycles through a list of responses; repeats the last once exhausted."""
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._idx = 0

    def respond(self, student_message: str, history: list[dict]) -> str:
        if self._idx < len(self._responses):
            r = self._responses[self._idx]
            self._idx += 1
            return r
        return self._responses[-1]


class _FixedStudent:
    """Duck-typed substitute for StudentAgent — returns fixed messages in order."""
    def __init__(self, messages: list[str]):
        self._msgs = list(messages)
        self._idx = 0

    def generate_message(self, tutor_msg: str, history: list) -> dict:
        if self._idx < len(self._msgs):
            m = self._msgs[self._idx]
            self._idx += 1
        else:
            m = self._msgs[-1] if self._msgs else "I see."
        return {"message": m}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def domain_map():
    return {
        "topic": "Test",
        "core_concepts": [{"concept": "X", "prerequisite_for": [], "knowledge_type": "concept"}],
        "recommended_sequence": ["X"],
    }


@pytest.fixture
def default_tutor():
    return _FixedTutor(["What do you think?"] * 30)


@pytest.fixture
def default_student():
    return _FixedStudent(["I don't know."] * 30)


def _run(tutor, student, domain_map, *, max_turns=3, min_turns=1, **kwargs):
    """Convenience wrapper."""
    return run_session(
        tutor=tutor,
        domain_map=domain_map,
        topic="Test",
        student_type="llm",
        student_agent=student,
        max_turns=max_turns,
        min_turns=min_turns,
        verbose=False,
        **kwargs,
    )


# ===========================================================================
# Transcript schema
# ===========================================================================

class TestTranscriptSchema:

    def test_schema_field_raw_transcript_v1(self, domain_map, default_tutor, default_student):
        t = _run(default_tutor, default_student, domain_map)
        assert t["_schema"] == "raw-transcript-v1"

    def test_required_top_level_fields_present(self, domain_map, default_tutor, default_student):
        t = _run(default_tutor, default_student, domain_map)
        for field in ("session_id", "topic", "domain_map", "turns", "bkt_initial_states", "_metadata"):
            assert field in t, f"Missing field: {field!r}"

    def test_metadata_has_required_keys(self, domain_map, default_tutor, default_student):
        t = _run(default_tutor, default_student, domain_map)
        meta = t["_metadata"]
        assert "total_tutor_turns" in meta
        assert "ended_by" in meta
        assert "is_valid" in meta

    def test_turns_list_is_non_empty(self, domain_map, default_tutor, default_student):
        t = _run(default_tutor, default_student, domain_map, max_turns=2)
        assert len(t["turns"]) > 0

    def test_first_turn_is_student_opening(self, domain_map, default_tutor, default_student):
        t = _run(default_tutor, default_student, domain_map)
        assert t["turns"][0]["role"] == "student"

    def test_turns_alternate_student_tutor(self, domain_map, default_tutor, default_student):
        t = _run(default_tutor, default_student, domain_map, max_turns=4)
        roles = [turn["role"] for turn in t["turns"]]
        for i in range(len(roles) - 1):
            assert roles[i] != roles[i + 1], f"Two consecutive {roles[i]!r} turns at indices {i}/{i+1}"

    def test_domain_map_embedded_in_transcript(self, domain_map, default_tutor, default_student):
        t = _run(default_tutor, default_student, domain_map)
        assert t["domain_map"] is domain_map

    def test_topic_stored(self, domain_map, default_tutor, default_student):
        t = _run(default_tutor, default_student, domain_map)
        assert t["topic"] == "Test"

    def test_explicit_session_id_preserved(self, domain_map, default_tutor, default_student):
        t = _run(default_tutor, default_student, domain_map, session_id="explicit-123")
        assert t["session_id"] == "explicit-123"

    def test_session_id_auto_generated_when_none(self, domain_map, default_tutor, default_student):
        t = _run(default_tutor, default_student, domain_map)
        assert t["session_id"]

    def test_source_stored(self, domain_map, default_tutor, default_student):
        t = _run(default_tutor, default_student, domain_map, source="gpt-4o")
        assert t["source"] == "gpt-4o"

    def test_default_opening_message_contains_topic(self, domain_map, default_tutor, default_student):
        t = _run(default_tutor, default_student, domain_map)
        first_content = t["turns"][0]["content"]
        assert "Test" in first_content

    def test_custom_opening_message_used(self, domain_map, default_tutor, default_student):
        t = _run(
            default_tutor, default_student, domain_map,
            opening_message="Custom opening!",
        )
        assert t["turns"][0]["content"] == "Custom opening!"


# ===========================================================================
# Termination conditions
# ===========================================================================

class TestTermination:

    def test_max_turns_sets_ended_by(self, domain_map, default_student):
        tutor = _FixedTutor(["Keep going."] * 10)
        t = _run(tutor, default_student, domain_map, max_turns=3)
        assert t["_metadata"]["ended_by"] == "max_turns"

    def test_max_turns_produces_correct_turn_count(self, domain_map, default_student):
        tutor = _FixedTutor(["Keep going."] * 10)
        t = _run(tutor, default_student, domain_map, max_turns=4)
        assert t["_metadata"]["total_tutor_turns"] == 4

    def test_tutor_session_complete_ends_early(self, domain_map, default_student):
        tutor = _FixedTutor(["Great work! [SESSION_COMPLETE]", "Should not reach this."])
        t = _run(tutor, default_student, domain_map, max_turns=10)
        assert t["_metadata"]["ended_by"] == "tutor"
        assert t["_metadata"]["total_tutor_turns"] == 1

    def test_student_session_complete_ends_early(self, domain_map):
        tutor = _FixedTutor(["What do you think?"] * 10)
        student = _FixedStudent(["I understand now [SESSION_COMPLETE]"])
        t = _run(tutor, student, domain_map, max_turns=10)
        assert t["_metadata"]["ended_by"] == "student"

    def test_session_complete_case_insensitive(self, domain_map, default_student):
        tutor = _FixedTutor(["[session_complete]"])
        t = _run(tutor, default_student, domain_map, max_turns=10)
        assert t["_metadata"]["ended_by"] == "tutor"

    def test_session_complete_embedded_in_longer_response(self, domain_map, default_student):
        tutor = _FixedTutor(["Excellent progress! You've mastered this. [SESSION_COMPLETE]"])
        t = _run(tutor, default_student, domain_map, max_turns=10)
        assert t["_metadata"]["ended_by"] == "tutor"


# ===========================================================================
# is_valid flag
# ===========================================================================

class TestValidity:

    def test_is_valid_true_when_turns_meet_minimum(self, domain_map, default_student):
        tutor = _FixedTutor(["Question."] * 20)
        t = _run(tutor, default_student, domain_map, max_turns=8, min_turns=8)
        assert t["_metadata"]["is_valid"] is True

    def test_is_valid_false_when_session_too_short(self, domain_map, default_student):
        tutor = _FixedTutor(["Done [SESSION_COMPLETE]"])
        t = _run(tutor, default_student, domain_map, max_turns=10, min_turns=8)
        assert t["_metadata"]["is_valid"] is False


# ===========================================================================
# Constructor-level validation
# ===========================================================================

class TestRunSessionValidation:

    def test_llm_student_type_without_agent_raises(self, domain_map):
        tutor = _FixedTutor(["hello"])
        with pytest.raises(ValueError, match="student_agent"):
            run_session(
                tutor=tutor,
                domain_map=domain_map,
                topic="Test",
                student_type="llm",
                student_agent=None,
                max_turns=3,
                min_turns=1,
                verbose=False,
            )

    def test_output_file_written(self, tmp_path, domain_map, default_tutor, default_student):
        out = tmp_path / "transcript.json"
        _run(default_tutor, default_student, domain_map, output_file=str(out))
        assert out.exists()
        import json
        data = json.loads(out.read_text())
        assert data["_schema"] == "raw-transcript-v1"
