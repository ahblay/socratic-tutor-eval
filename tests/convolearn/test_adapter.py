"""
tests/convolearn/test_adapter.py

Unit tests for convolearn/adapter.py.
No API calls — tests pure-Python conversation parsing and the thin wrapper
over prepare_analysis_input().
Covers:
  - _parse_conversation
  - adapt_dialogue (integration with prepare_analysis_input)
"""

import pytest

from convolearn.adapter import _parse_conversation, adapt_dialogue


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_domain_map():
    return {
        "topic": "Solar energy",
        "core_concepts": [
            {"concept": "Insolation",   "prerequisite_for": ["Angle of incidence"], "knowledge_type": "concept"},
            {"concept": "Angle of incidence", "prerequisite_for": [], "knowledge_type": "concept"},
        ],
        "recommended_sequence": ["Insolation", "Angle of incidence"],
    }


@pytest.fixture
def sample_dialogue():
    return {
        "dialogue_idx": 3,
        "cleaned_conversation": (
            "Student: Why does the equator get more sun?\n"
            "Teacher: What shape is the Earth?\n"
            "Student: Spherical.\n"
            "Teacher: And how does that affect sunlight hitting the surface?\n"
        ),
        "effectiveness_consensus": 4.0,
        "completeness_consensus": 2.5,
        "num_exchanges": 11,
    }


# ===========================================================================
# _parse_conversation
# ===========================================================================

class TestParseConversation:

    def test_single_student_turn(self):
        turns = _parse_conversation("Student: Hello?")
        assert len(turns) == 1
        assert turns[0]["role"] == "student"
        assert turns[0]["content"] == "Hello?"

    def test_single_teacher_turn(self):
        turns = _parse_conversation("Teacher: What do you think?")
        assert len(turns) == 1
        assert turns[0]["role"] == "teacher"
        assert turns[0]["content"] == "What do you think?"

    def test_alternating_turns_preserved_in_order(self):
        conv = (
            "Student: Why?\n"
            "Teacher: Think about it.\n"
            "Student: I don't know.\n"
            "Teacher: What do you observe?\n"
        )
        turns = _parse_conversation(conv)
        assert len(turns) == 4
        assert turns[0]["role"] == "student"
        assert turns[1]["role"] == "teacher"
        assert turns[2]["role"] == "student"
        assert turns[3]["role"] == "teacher"

    def test_role_labels_are_lowercase(self):
        conv = "Student: Hi.\nTeacher: Hello."
        turns = _parse_conversation(conv)
        assert turns[0]["role"] == "student"
        assert turns[1]["role"] == "teacher"

    def test_multiline_teacher_response_joined_to_single_turn(self):
        conv = (
            "Student: Why?\n"
            "Teacher: First line.\n"
            "This continues the teacher response.\n"
            "And so does this.\n"
            "Student: I see.\n"
        )
        turns = _parse_conversation(conv)
        assert len(turns) == 3
        teacher_turn = turns[1]
        assert teacher_turn["role"] == "teacher"
        assert "First line." in teacher_turn["content"]
        assert "This continues" in teacher_turn["content"]
        assert "And so does" in teacher_turn["content"]

    def test_multiline_student_question_joined(self):
        conv = (
            "Student: I have a question\n"
            "about solar energy and\n"
            "the equator specifically.\n"
            "Teacher: Good question.\n"
        )
        turns = _parse_conversation(conv)
        assert turns[0]["role"] == "student"
        assert "equator specifically" in turns[0]["content"]

    def test_empty_lines_ignored_in_continuation(self):
        conv = "Student: Hello?\n\nTeacher: Hi there."
        turns = _parse_conversation(conv)
        # Empty line is not a new turn; content of teacher turn should be "Hi there."
        teacher_turns = [t for t in turns if t["role"] == "teacher"]
        assert len(teacher_turns) == 1
        assert teacher_turns[0]["content"] == "Hi there."

    def test_empty_string_returns_empty_list(self):
        assert _parse_conversation("") == []

    def test_content_stripped_of_whitespace(self):
        conv = "Student:   padded question   \nTeacher:   padded answer   "
        turns = _parse_conversation(conv)
        assert turns[0]["content"] == "padded question"
        assert turns[1]["content"] == "padded answer"

    def test_turns_with_no_content_excluded(self):
        # A role label with nothing after it and no continuation lines
        conv = "Student:\nTeacher: Hello."
        turns = _parse_conversation(conv)
        roles = [t["role"] for t in turns]
        # The empty student turn should be dropped
        assert "student" not in roles or all(t["content"] for t in turns if t["role"] == "student")

    def test_realistic_convolearn_sample(self):
        conv = (
            "Student: How do ocean currents affect coastal climates?\n"
            "Teacher: What do you know about the temperature of the Gulf Stream?\n"
            "Student: It's warm, coming from the Gulf of Mexico.\n"
            "Teacher: Right. So how might that warm water affect nearby air?\n"
            "Student: The warm water heats the air above it?\n"
            "Teacher: Exactly. And how does warmer air behave compared to cold air?\n"
        )
        turns = _parse_conversation(conv)
        assert len(turns) == 6
        assert turns[0]["role"] == "student"
        assert turns[-1]["role"] == "teacher"


# ===========================================================================
# adapt_dialogue
# ===========================================================================

class TestAdaptDialogue:

    def test_session_id_format(self, sample_dialogue, minimal_domain_map):
        out = adapt_dialogue(
            prompt_id="equator-sun",
            question_prompt="Why does the equator get more sun?",
            dialogue=sample_dialogue,
            domain_map=minimal_domain_map,
        )
        assert out["session_id"] == "equator-sun_3"

    def test_session_id_uses_dialogue_idx(self, minimal_domain_map):
        d = {"dialogue_idx": 17, "cleaned_conversation": "Student: Hi.\nTeacher: Hello.", "effectiveness_consensus": 3.0, "completeness_consensus": 2.0, "num_exchanges": 5}
        out = adapt_dialogue("my-prompt", "Hi.", d, minimal_domain_map)
        assert out["session_id"] == "my-prompt_17"

    def test_teacher_role_becomes_tutor_in_output(self, sample_dialogue, minimal_domain_map):
        out = adapt_dialogue("p", "q", sample_dialogue, minimal_domain_map)
        roles = {t["role"] for t in out["lesson_turns"]}
        assert "teacher" not in roles
        assert "tutor" in roles

    def test_student_role_becomes_user_in_output(self, sample_dialogue, minimal_domain_map):
        out = adapt_dialogue("p", "q", sample_dialogue, minimal_domain_map)
        roles = {t["role"] for t in out["lesson_turns"]}
        assert "student" not in roles
        assert "user" in roles

    def test_turn_count_matches_parsed_conversation(self, sample_dialogue, minimal_domain_map):
        turns_parsed = _parse_conversation(sample_dialogue["cleaned_conversation"])
        out = adapt_dialogue("p", "q", sample_dialogue, minimal_domain_map)
        assert len(out["lesson_turns"]) == len(turns_parsed)

    def test_article_title_is_question_prompt(self, sample_dialogue, minimal_domain_map):
        qp = "Why does the equator get more sun?"
        out = adapt_dialogue("equator-sun", qp, sample_dialogue, minimal_domain_map)
        assert out["article_title"] == qp

    def test_domain_map_preserved_in_output(self, sample_dialogue, minimal_domain_map):
        out = adapt_dialogue("p", "q", sample_dialogue, minimal_domain_map)
        assert out["domain_map"] == minimal_domain_map

    def test_bkt_initial_states_populated_from_domain_map(self, sample_dialogue, minimal_domain_map):
        out = adapt_dialogue("p", "q", sample_dialogue, minimal_domain_map, bkt_preset="absent")
        assert out["bkt_initial_states"]
        for state in out["bkt_initial_states"].values():
            assert state["p_mastered"] == pytest.approx(0.10)

    def test_bkt_preset_all_partial_honoured(self, sample_dialogue, minimal_domain_map):
        out = adapt_dialogue("p", "q", sample_dialogue, minimal_domain_map, bkt_preset="all_partial")
        for state in out["bkt_initial_states"].values():
            assert state["p_mastered"] == pytest.approx(0.50)

    def test_assessment_turns_always_empty(self, sample_dialogue, minimal_domain_map):
        out = adapt_dialogue("p", "q", sample_dialogue, minimal_domain_map)
        assert out["assessment_turns"] == []

    def test_turn_numbers_sequential_from_one(self, sample_dialogue, minimal_domain_map):
        out = adapt_dialogue("p", "q", sample_dialogue, minimal_domain_map)
        nums = [t["turn_number"] for t in out["lesson_turns"]]
        assert nums == list(range(1, len(out["lesson_turns"]) + 1))

    def test_empty_conversation_produces_no_lesson_turns(self, minimal_domain_map):
        d = {"dialogue_idx": 0, "cleaned_conversation": "", "effectiveness_consensus": 3.0, "completeness_consensus": 2.0, "num_exchanges": 0}
        out = adapt_dialogue("p", "q", d, minimal_domain_map)
        assert out["lesson_turns"] == []
