"""
tests/tutor_eval/test_socratic_tutor.py

Unit tests for SocraticTutor — state management and serialization.
No API calls made (respond() is not called).
"""

import pytest
from tutor_eval.tutors.socratic import SocraticTutor
from webapp.services.domain_cache import build_kg_from_domain_map, get_target_kcs


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
