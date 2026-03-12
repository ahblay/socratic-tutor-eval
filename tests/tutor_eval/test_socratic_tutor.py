"""
tests/tutor_eval/test_socratic_tutor.py

Unit tests for SocraticTutor — state management and serialization.
No API calls made (respond() is not called).
"""

import pytest
from tutor_eval.tutors.socratic import SocraticTutor
from webapp.services.domain_cache import build_kg_from_domain_map, get_target_kcs


from unittest.mock import MagicMock, patch

SAMPLE_DOMAIN_MAP = {
    "topic": "DNA",
    "core_concepts": [
        {"concept": "DNA Structure", "description": "...", "prerequisite_for": ["DNA Replication"], "depth_priority": "essential"},
        {"concept": "DNA Replication", "description": "...", "prerequisite_for": [], "depth_priority": "essential"},
    ],
    "recommended_sequence": ["DNA Structure", "DNA Replication"],
    "common_misconceptions": [],
    "checkpoint_questions": [],
    "required_skills": [],
    "prerequisite_knowledge": [],
    "engagement_risk_points": [],
}


class TestSocraticTutorStateInit:
    def test_default_state(self):
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        assert tutor._state["current_phase"] == 1
        assert tutor._state["turn_count"] == 0
        assert tutor._state["frustration_level"] == "none"
        assert tutor._state["learning_style"] is None

    def test_state_injection(self):
        saved = {
            "current_phase": 4,
            "current_concept_index": 1,
            "student_understanding": ["understands structure"],
            "learning_style": "example-driven",
            "frustration_level": "mild",
            "turn_count": 9,
            "accuracy_issues_open": [],
        }
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP, state=saved)
        assert tutor._state["current_phase"] == 4
        assert tutor._state["turn_count"] == 9
        assert tutor._state["learning_style"] == "example-driven"

    def test_injected_state_is_copied(self):
        """Mutating the original dict should not affect tutor state."""
        saved = {"current_phase": 2, "current_concept_index": 0,
                 "student_understanding": [], "learning_style": None,
                 "frustration_level": "none", "turn_count": 3,
                 "accuracy_issues_open": []}
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP, state=saved)
        saved["current_phase"] = 99
        assert tutor._state["current_phase"] == 2

    def test_session_state_returns_copy(self):
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        snap = tutor.session_state()
        snap["current_phase"] = 99
        assert tutor._state["current_phase"] == 1

    def test_roundtrip_serialization(self):
        """session_state() output can reconstruct an identical tutor."""
        original = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        original._state["current_phase"] = 3
        original._state["turn_count"] = 7
        original._state["learning_style"] = "conceptual"

        snap = original.session_state()
        restored = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP, state=snap)

        assert restored._state["current_phase"] == 3
        assert restored._state["turn_count"] == 7
        assert restored._state["learning_style"] == "conceptual"


# ---------------------------------------------------------------------------
# Domain cache helpers
# ---------------------------------------------------------------------------

class TestBuildKgFromDomainMap:
    def test_kcs_created(self):
        kg = build_kg_from_domain_map(SAMPLE_DOMAIN_MAP)
        ids = {kc["id"] for kc in kg["kcs"]}
        assert "dna-structure" in ids
        assert "dna-replication" in ids

    def test_edges_created(self):
        kg = build_kg_from_domain_map(SAMPLE_DOMAIN_MAP)
        # DNA Structure → DNA Replication
        assert {"from": "dna-structure", "to": "dna-replication"} in kg["edges"]

    def test_no_self_edges(self):
        kg = build_kg_from_domain_map(SAMPLE_DOMAIN_MAP)
        for edge in kg["edges"]:
            assert edge["from"] != edge["to"]

    def test_target_kcs_order(self):
        target = get_target_kcs(SAMPLE_DOMAIN_MAP)
        assert target == ["dna-structure", "dna-replication"]

    def test_empty_domain_map(self):
        kg = build_kg_from_domain_map({})
        assert kg == {"kcs": [], "edges": []}

    def test_slug_normalisation(self):
        dm = {
            "core_concepts": [
                {"concept": "Hello World!!", "prerequisite_for": [], "description": ""},
            ],
            "recommended_sequence": ["Hello World!!"],
        }
        kg = build_kg_from_domain_map(dm)
        assert kg["kcs"][0]["id"] == "hello-world"


# ---------------------------------------------------------------------------
# Prompt caching — system prompt structure
# ---------------------------------------------------------------------------

class TestPromptCaching:
    def _make_mock_client(self, reply_text="What do you think?", reviewer_verdict="PASS"):
        """Return a mock Anthropic client that records calls."""
        mock_client = MagicMock()
        # First call: tutor response
        tutor_response = MagicMock()
        tutor_response.content = [MagicMock(text=reply_text)]
        # Second call: response reviewer
        reviewer_response = MagicMock()
        reviewer_response.content = [MagicMock(text=reviewer_verdict)]
        mock_client.messages.create.side_effect = [tutor_response, reviewer_response]
        return mock_client

    def test_domain_map_in_system_not_messages(self):
        """Domain map must appear in system blocks, not in the messages list."""
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        tutor.client = self._make_mock_client()

        tutor.respond("What is DNA?", [{"role": "student", "text": "What is DNA?"}])

        call_kwargs = tutor.client.messages.create.call_args_list[0][1]
        system = call_kwargs["system"]

        # system must be a list of blocks
        assert isinstance(system, list)
        assert len(system) == 2

        # Second block contains domain map and has cache_control
        domain_block = system[1]
        assert "DOMAIN MAP" in domain_block["text"]
        assert "DNA Structure" in domain_block["text"]
        assert domain_block.get("cache_control") == {"type": "ephemeral"}

        # Domain map JSON must NOT be in the messages list (only the concept
        # name may appear in session state — check for JSON-specific content)
        messages = call_kwargs["messages"]
        messages_text = " ".join(
            m["content"] for m in messages if isinstance(m.get("content"), str)
        )
        assert '"prerequisite_for"' not in messages_text  # JSON key only in domain map
        assert '"depth_priority"' not in messages_text

    def test_session_state_in_messages_not_system(self):
        """Session state header must be in messages injection, not system blocks."""
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        tutor.client = self._make_mock_client()

        tutor.respond("Hello", [{"role": "student", "text": "Hello"}])

        call_kwargs = tutor.client.messages.create.call_args_list[0][1]
        system_text = " ".join(b["text"] for b in call_kwargs["system"])
        messages_text = " ".join(
            m["content"] for m in call_kwargs["messages"]
            if isinstance(m.get("content"), str)
        )

        # The ## SESSION STATE header is injected per-turn into messages
        assert "## SESSION STATE" in messages_text
        # The system blocks should not contain the per-turn header
        assert "## SESSION STATE" not in system_text


# ---------------------------------------------------------------------------
# Response guardrail
# ---------------------------------------------------------------------------

class TestResponseGuardrail:
    def _tutor_with_mock(self, tutor_reply: str, reviewer_verdict: str):
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        mock_client = MagicMock()
        tutor_resp = MagicMock()
        tutor_resp.content = [MagicMock(text=tutor_reply)]
        reviewer_resp = MagicMock()
        reviewer_resp.content = [MagicMock(text=reviewer_verdict)]
        mock_client.messages.create.side_effect = [tutor_resp, reviewer_resp]
        tutor.client = mock_client
        return tutor

    def test_pass_returns_original_response(self):
        tutor = self._tutor_with_mock(
            tutor_reply="What do you already know about this?",
            reviewer_verdict="PASS",
        )
        result = tutor.respond("What is DNA?", [{"role": "student", "text": "What is DNA?"}])
        assert result == "What do you already know about this?"

    def test_fail_returns_rewritten_response(self):
        tutor = self._tutor_with_mock(
            tutor_reply="DNA is a double helix made of nucleotides.",
            reviewer_verdict="FAIL: What do you think DNA might be made of?",
        )
        result = tutor.respond("What is DNA?", [{"role": "student", "text": "What is DNA?"}])
        assert result == "What do you think DNA might be made of?"
        assert "double helix" not in result

    def test_reviewer_called_on_every_turn(self):
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        mock_client = MagicMock()
        def make_resp(text):
            r = MagicMock(); r.content = [MagicMock(text=text)]; return r
        mock_client.messages.create.side_effect = [
            make_resp("Question 1?"), make_resp("PASS"),
            make_resp("Question 2?"), make_resp("PASS"),
        ]
        tutor.client = mock_client

        tutor.respond("Hi", [{"role": "student", "text": "Hi"}])
        tutor.respond("Okay", [{"role": "student", "text": "Okay"}])

        # 4 total calls: 2 tutor + 2 reviewer
        assert mock_client.messages.create.call_count == 4

    def test_reviewer_failure_does_not_crash(self):
        """If the reviewer raises an exception, the original reply is returned."""
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        mock_client = MagicMock()
        tutor_resp = MagicMock()
        tutor_resp.content = [MagicMock(text="Here is the answer: 42.")]
        mock_client.messages.create.side_effect = [
            tutor_resp,
            Exception("API timeout"),
        ]
        tutor.client = mock_client

        result = tutor.respond("What is the answer?", [{"role": "student", "text": "What is the answer?"}])
        assert result == "Here is the answer: 42."  # falls back gracefully
