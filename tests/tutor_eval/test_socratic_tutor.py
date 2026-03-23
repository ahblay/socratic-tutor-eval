"""
tests/tutor_eval/test_socratic_tutor.py

Unit tests for SocraticTutor — state management and serialization.
No API calls made (respond() is not called).
"""

import json

import pytest
from tutor_eval.tutors.socratic import SocraticTutor, enrich_domain_map
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
    def _make_text_block(self, text):
        """Mock a TextBlock as returned by the Anthropic SDK."""
        block = MagicMock()
        block.type = "text"
        block.text = text
        return block

    def _make_mock_client(self, reply_text="What do you think?", reviewer_verdict='{"verdict": "pass"}'):
        """Return a mock Anthropic client that records calls."""
        mock_client = MagicMock()
        # First call: tutor response (TextBlock only — no thinking block in tests)
        tutor_response = MagicMock()
        tutor_response.content = [self._make_text_block(reply_text)]
        # Second call: response reviewer
        reviewer_response = MagicMock()
        reviewer_response.content = [self._make_text_block(reviewer_verdict)]
        mock_client.messages.create.side_effect = [tutor_response, reviewer_response]
        return mock_client

    def test_domain_map_in_system_not_messages(self):
        """Domain map must appear in system blocks, not in the messages list."""
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        tutor.client = self._make_mock_client()

        tutor.respond("What is DNA?", [{"role": "student", "text": "What is DNA?"}])

        call_kwargs = tutor.client.messages.create.call_args_list[0][1]
        system = call_kwargs["system"]

        # system must be a list of 3 blocks: rules + domain map + session state
        assert isinstance(system, list)
        assert len(system) == 3

        # Second block contains domain map and has cache_control
        domain_block = system[1]
        assert "DOMAIN MAP" in domain_block["text"]
        assert "DNA Structure" in domain_block["text"]
        assert domain_block.get("cache_control") == {"type": "ephemeral"}

        # Domain map JSON must NOT be in the messages list
        messages = call_kwargs["messages"]
        messages_text = " ".join(
            m["content"] for m in messages if isinstance(m.get("content"), str)
        )
        assert '"prerequisite_for"' not in messages_text  # JSON key only in domain map
        assert '"depth_priority"' not in messages_text

    def test_session_state_in_system_not_messages(self):
        """Session state header must be in the third system block, not in messages."""
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        tutor.client = self._make_mock_client()

        tutor.respond("Hello", [{"role": "student", "text": "Hello"}])

        call_kwargs = tutor.client.messages.create.call_args_list[0][1]
        system_text = " ".join(b["text"] for b in call_kwargs["system"])
        messages_text = " ".join(
            m["content"] for m in call_kwargs["messages"]
            if isinstance(m.get("content"), str)
        )

        # The ## SESSION STATE header is now in the system prompt (third block)
        assert "## SESSION STATE" in system_text
        # It must not appear in the messages list
        assert "## SESSION STATE" not in messages_text


# ---------------------------------------------------------------------------
# Response guardrail
# ---------------------------------------------------------------------------

class TestResponseGuardrail:
    def _make_text_block(self, text):
        block = MagicMock()
        block.type = "text"
        block.text = text
        return block

    def _tutor_with_mock(self, tutor_reply: str, reviewer_verdict: str):
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        mock_client = MagicMock()
        tutor_resp = MagicMock()
        tutor_resp.content = [self._make_text_block(tutor_reply)]
        reviewer_resp = MagicMock()
        reviewer_resp.content = [self._make_text_block(reviewer_verdict)]
        mock_client.messages.create.side_effect = [tutor_resp, reviewer_resp]
        tutor.client = mock_client
        return tutor

    def test_pass_returns_original_response(self):
        tutor = self._tutor_with_mock(
            tutor_reply="What do you already know about this?",
            reviewer_verdict='{"verdict": "pass"}',
        )
        result = tutor.respond("What is DNA?", [{"role": "student", "text": "What is DNA?"}])
        assert result == "What do you already know about this?"

    def test_fail_triggers_correction_reprompt(self):
        """On fail, the tutor is reprompted and the corrected reply is returned."""
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            MagicMock(content=[self._make_text_block("DNA is a double helix made of nucleotides.")]),
            MagicMock(content=[self._make_text_block('{"verdict": "fail", "violation": "states answer", "suggestion": "What do you think DNA might be made of?"}')]),
            MagicMock(content=[self._make_text_block("What do you think DNA might be made of?")]),
        ]
        tutor.client = mock_client
        result = tutor.respond("What is DNA?", [{"role": "student", "text": "What is DNA?"}])
        assert result == "What do you think DNA might be made of?"
        assert mock_client.messages.create.call_count == 3

    def test_warn_returns_original_and_stores_verdict(self):
        """On warn, the original reply is returned and the verdict is stored."""
        tutor = self._tutor_with_mock(
            tutor_reply="That's an interesting way to put it — what else do you notice?",
            reviewer_verdict='{"verdict": "warn", "violation": "mild affirmation"}',
        )
        result = tutor.respond("DNA has two strands?", [{"role": "student", "text": "DNA has two strands?"}])
        assert result == "That's an interesting way to put it — what else do you notice?"
        assert tutor._last_reviewer_verdict == "warn"
        assert tutor._last_reviewer_violation == "mild affirmation"

    def test_reviewer_called_on_every_turn(self):
        tutor = SocraticTutor("DNA", SAMPLE_DOMAIN_MAP)
        mock_client = MagicMock()
        def make_resp(text):
            r = MagicMock()
            block = MagicMock()
            block.type = "text"
            block.text = text
            r.content = [block]
            return r
        mock_client.messages.create.side_effect = [
            make_resp("Question 1?"), make_resp('{"verdict": "pass"}'),
            make_resp("Question 2?"), make_resp('{"verdict": "pass"}'),
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
        block = MagicMock()
        block.type = "text"
        block.text = "Here is the answer: 42."
        tutor_resp.content = [block]
        mock_client.messages.create.side_effect = [
            tutor_resp,
            Exception("API timeout"),
        ]
        tutor.client = mock_client

        result = tutor.respond("What is the answer?", [{"role": "student", "text": "What is the answer?"}])
        assert result == "Here is the answer: 42."  # falls back gracefully

    def test_knowledge_type_injected_in_reviewer_prompt(self):
        """Reviewer prompt contains the current concept's knowledge_type."""
        convention_domain_map = {
            "topic": "DNA",
            "core_concepts": [
                {
                    "concept": "DNA Structure",
                    "description": "...",
                    "prerequisite_for": [],
                    "depth_priority": "essential",
                    "knowledge_type": "convention",
                    "reference_material": "DNA uses A, T, G, C bases.",
                },
            ],
            "recommended_sequence": ["DNA Structure"],
            "common_misconceptions": [],
            "checkpoint_questions": [],
            "required_skills": [],
            "prerequisite_knowledge": [],
            "engagement_risk_points": [],
        }
        tutor = SocraticTutor("DNA", convention_domain_map)
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            MagicMock(content=[self._make_text_block("Before we go further, here is something you will need: DNA uses A, T, G, C bases. Which base pairs with which?")]),
            MagicMock(content=[self._make_text_block('{"verdict": "pass"}')]),
        ]
        tutor.client = mock_client

        tutor.respond("Tell me about DNA.", [{"role": "student", "text": "Tell me about DNA."}])

        reviewer_call_kwargs = mock_client.messages.create.call_args_list[1][1]
        reviewer_prompt = reviewer_call_kwargs["messages"][0]["content"]
        assert "convention" in reviewer_prompt


# ---------------------------------------------------------------------------
# enrich_domain_map
# ---------------------------------------------------------------------------

_ENRICHED_DOMAIN_MAP = {
    "topic": "DNA",
    "core_concepts": [
        {
            "concept": "DNA Structure",
            "description": "...",
            "prerequisite_for": ["DNA Replication"],
            "depth_priority": "essential",
            "knowledge_type": "concept",
            "reference_material": "Imagine a twisted ladder — what do you think each part of the ladder might represent?",
        },
        {
            "concept": "DNA Replication",
            "description": "...",
            "prerequisite_for": [],
            "depth_priority": "essential",
            "knowledge_type": "concept",
            "reference_material": "A cell needs to divide. What has to happen to the genetic information before that can occur?",
        },
    ],
    "recommended_sequence": ["DNA Structure", "DNA Replication"],
    "common_misconceptions": [],
    "checkpoint_questions": [],
    "required_skills": [],
    "prerequisite_knowledge": [],
    "engagement_risk_points": [],
}


class TestEnrichDomainMap:
    def _mock_client(self, response_text: str) -> MagicMock:
        client = MagicMock()
        resp = MagicMock()
        block = MagicMock()
        block.text = response_text
        resp.content = [block]
        client.messages.create.return_value = resp
        return client

    def test_adds_knowledge_type_and_reference_material(self):
        client = self._mock_client(json.dumps(_ENRICHED_DOMAIN_MAP))
        result = enrich_domain_map(SAMPLE_DOMAIN_MAP, client)
        for concept in result["core_concepts"]:
            assert "knowledge_type" in concept
            assert concept["knowledge_type"] in ("convention", "concept", "narrative")
            assert "reference_material" in concept
            assert isinstance(concept["reference_material"], str)

    def test_may_expand_concept_count(self):
        """Enricher is allowed to split concepts — result may have more nodes."""
        expanded = dict(_ENRICHED_DOMAIN_MAP)
        expanded["core_concepts"] = _ENRICHED_DOMAIN_MAP["core_concepts"] + [
            {
                "concept": "DNA Repair",
                "description": "...",
                "prerequisite_for": [],
                "depth_priority": "important",
                "knowledge_type": "concept",
                "reference_material": "What happens if a copying error is made?",
            }
        ]
        expanded["recommended_sequence"] = ["DNA Structure", "DNA Replication", "DNA Repair"]
        client = self._mock_client(json.dumps(expanded))
        result = enrich_domain_map(SAMPLE_DOMAIN_MAP, client)
        assert len(result["core_concepts"]) == 3

    def test_falls_back_on_api_error(self):
        client = MagicMock()
        client.messages.create.side_effect = Exception("network error")
        result = enrich_domain_map(SAMPLE_DOMAIN_MAP, client)
        assert result == SAMPLE_DOMAIN_MAP

    def test_falls_back_on_invalid_json(self):
        client = self._mock_client("not valid json at all")
        result = enrich_domain_map(SAMPLE_DOMAIN_MAP, client)
        assert result == SAMPLE_DOMAIN_MAP

    def test_falls_back_on_empty_concepts(self):
        bad = {"topic": "DNA", "core_concepts": [], "recommended_sequence": []}
        client = self._mock_client(json.dumps(bad))
        result = enrich_domain_map(SAMPLE_DOMAIN_MAP, client)
        assert result == SAMPLE_DOMAIN_MAP

    def test_falls_back_if_concept_missing_name(self):
        bad = {
            "topic": "DNA",
            "core_concepts": [{"description": "no concept key", "prerequisite_for": []}],
            "recommended_sequence": ["something"],
        }
        client = self._mock_client(json.dumps(bad))
        result = enrich_domain_map(SAMPLE_DOMAIN_MAP, client)
        assert result == SAMPLE_DOMAIN_MAP

    def test_strips_markdown_fences(self):
        fenced = "```json\n" + json.dumps(_ENRICHED_DOMAIN_MAP) + "\n```"
        client = self._mock_client(fenced)
        result = enrich_domain_map(SAMPLE_DOMAIN_MAP, client)
        assert result["topic"] == "DNA"
        assert "knowledge_type" in result["core_concepts"][0]
