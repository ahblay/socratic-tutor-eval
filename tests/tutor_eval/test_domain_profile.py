"""
tests/tutor_eval/test_domain_profile.py

Unit tests for tutor_eval/student/domain_profile.py.
No API calls.
"""

import pytest
from tutor_eval.student.domain_profile import (
    _derive_slug,
    _match_misconception_to_kc,
    build_kg_from_domain_map,
    generate_profile,
    bkt_states_from_profile,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def linear_map():
    """Linear chain: Foundations → Core → Advanced.
    Foundations is the single root KC (no incoming edge).
    """
    return {
        "topic": "Test Topic",
        "core_concepts": [
            {"concept": "Foundations", "prerequisite_for": ["Core"],     "knowledge_type": "concept"},
            {"concept": "Core",        "prerequisite_for": ["Advanced"], "knowledge_type": "concept"},
            {"concept": "Advanced",    "prerequisite_for": [],           "knowledge_type": "concept"},
        ],
        "recommended_sequence": ["Foundations", "Core", "Advanced"],
        "common_misconceptions": [
            "Students confuse Foundations with Core",
            "Core concepts are often misunderstood as Advanced",
        ],
    }


@pytest.fixture
def two_root_map():
    """Two independent roots, each with one child."""
    return {
        "topic": "Two Roots",
        "core_concepts": [
            {"concept": "Root A", "prerequisite_for": ["Child A"], "knowledge_type": "concept"},
            {"concept": "Root B", "prerequisite_for": ["Child B"], "knowledge_type": "concept"},
            {"concept": "Child A", "prerequisite_for": [], "knowledge_type": "concept"},
            {"concept": "Child B", "prerequisite_for": [], "knowledge_type": "concept"},
        ],
        "recommended_sequence": ["Root A", "Root B", "Child A", "Child B"],
        "common_misconceptions": [],
    }


# ===========================================================================
# _derive_slug
# ===========================================================================

class TestDeriveSlug:

    def test_slug_matches_converter_slug(self):
        """domain_profile._derive_slug must stay in sync with converter._derive_slug."""
        from tutor_eval.ingestion.converter import _derive_slug as converter_slug
        for name in ["Nash Equilibrium", "Von Neumann–Morgenstern", "Repeated Games"]:
            assert _derive_slug(name) == converter_slug(name), (
                f"Slug mismatch for {name!r}: profile={_derive_slug(name)!r} "
                f"converter={converter_slug(name)!r}"
            )

    def test_lowercase(self):
        assert _derive_slug("UPPER CASE") == "upper-case"

    def test_spaces_become_hyphens(self):
        assert _derive_slug("hello world") == "hello-world"

    def test_special_chars_stripped(self):
        assert _derive_slug("hello, world!") == "hello-world"

    def test_truncated_at_64(self):
        assert len(_derive_slug("a" * 100)) == 64

    def test_no_leading_trailing_hyphens(self):
        result = _derive_slug("  --foo--  ")
        assert not result.startswith("-")
        assert not result.endswith("-")


# ===========================================================================
# build_kg_from_domain_map
# ===========================================================================

class TestBuildKgFromDomainMap:

    def test_kcs_extracted(self, linear_map):
        kg = build_kg_from_domain_map(linear_map)
        ids = {kc["id"] for kc in kg["kcs"]}
        assert {"foundations", "core", "advanced"} == ids

    def test_edges_from_prerequisite_for(self, linear_map):
        kg = build_kg_from_domain_map(linear_map)
        pairs = {(e["from"], e["to"]) for e in kg["edges"]}
        assert ("foundations", "core") in pairs
        assert ("core", "advanced") in pairs

    def test_no_self_edges(self, linear_map):
        kg = build_kg_from_domain_map(linear_map)
        assert not any(e["from"] == e["to"] for e in kg["edges"])

    def test_empty_domain_map_returns_empty_kg(self):
        kg = build_kg_from_domain_map({})
        assert kg == {"kcs": [], "edges": []}

    def test_kc_names_preserved(self, linear_map):
        kg = build_kg_from_domain_map(linear_map)
        names = {kc["name"] for kc in kg["kcs"]}
        assert "Foundations" in names


# ===========================================================================
# _match_misconception_to_kc
# ===========================================================================

class TestMatchMisconceptionToKc:

    def test_matches_by_word_overlap(self):
        kcs = [{"id": "foundations", "name": "Foundations"}, {"id": "core", "name": "Core"}]
        result = _match_misconception_to_kc("Students confuse Foundations", kcs)
        assert result == "foundations"

    def test_falls_back_to_first_kc_on_no_match(self):
        kcs = [{"id": "alpha", "name": "Alpha"}, {"id": "beta", "name": "Beta"}]
        result = _match_misconception_to_kc("completely unrelated text xyz", kcs)
        assert result == "alpha"

    def test_empty_kcs_returns_unknown(self):
        result = _match_misconception_to_kc("some misconception", [])
        assert result == "unknown"


# ===========================================================================
# generate_profile presets
# ===========================================================================

class TestGenerateProfile:

    def test_novice_roots_partial_rest_absent(self, linear_map):
        profile, _ = generate_profile(linear_map, preset="novice")
        assert "foundations" in profile["partial"]
        assert "core"        in profile["absent"]
        assert "advanced"    in profile["absent"]
        assert profile["mastered"] == []

    def test_expert_all_kcs_mastered(self, linear_map):
        profile, kg = generate_profile(linear_map, preset="expert")
        all_ids = {kc["id"] for kc in kg["kcs"]}
        assert set(profile["mastered"]) == all_ids
        assert profile["partial"] == []
        assert profile["absent"] == []

    def test_partial_knowledge_midpoint_split(self, linear_map):
        profile, kg = generate_profile(linear_map, preset="partial_knowledge")
        total = len(profile["mastered"]) + len(profile["partial"]) + len(profile["absent"])
        assert total == len(kg["kcs"])
        # At least one KC mastered (root), and not all mastered
        assert profile["mastered"]
        assert len(profile["mastered"]) < len(kg["kcs"])

    def test_misconception_heavy_root_mastered_first_two_non_root_partial(self, linear_map):
        profile, _ = generate_profile(linear_map, preset="misconception_heavy")
        assert "foundations" in profile["mastered"]
        assert len(profile["partial"]) <= 2

    def test_invalid_preset_raises_value_error(self, linear_map):
        with pytest.raises(ValueError, match="Unknown preset"):
            generate_profile(linear_map, preset="genius")

    def test_empty_concepts_returns_empty_profile(self):
        dm = {"topic": "empty", "core_concepts": []}
        profile, kg = generate_profile(dm, preset="novice")
        assert profile["mastered"] == []
        assert profile["absent"] == []
        assert kg["kcs"] == []

    def test_misconception_count_injects_entries(self, linear_map):
        profile, _ = generate_profile(linear_map, preset="novice", misconception_count=1)
        assert len(profile["misconceptions"]) == 1

    def test_misconception_count_capped_by_available(self, linear_map):
        profile, _ = generate_profile(linear_map, preset="novice", misconception_count=100)
        # Only 2 misconceptions defined in linear_map fixture
        assert len(profile["misconceptions"]) == 2

    def test_no_misconceptions_by_default(self, linear_map):
        profile, _ = generate_profile(linear_map, preset="novice")
        assert profile["misconceptions"] == []

    def test_misconception_has_kc_and_description_keys(self, linear_map):
        profile, _ = generate_profile(linear_map, preset="novice", misconception_count=1)
        m = profile["misconceptions"][0]
        assert "kc" in m
        assert "description" in m

    def test_base_model_stored_in_profile(self, linear_map):
        profile, _ = generate_profile(linear_map, preset="novice", base_model="sonnet")
        assert profile["base_model"] == "sonnet"

    def test_all_profile_kcs_exist_in_kg(self, linear_map):
        """No KC referenced in the profile should be absent from the KG."""
        profile, kg = generate_profile(linear_map, preset="partial_knowledge")
        kg_ids = {kc["id"] for kc in kg["kcs"]}
        for kc_id in profile["mastered"] + profile["partial"] + profile["absent"]:
            assert kc_id in kg_ids, f"KC {kc_id!r} in profile but not in KG"

    def test_two_root_map_novice_both_roots_partial(self, two_root_map):
        profile, kg = generate_profile(two_root_map, preset="novice")
        assert _derive_slug("Root A") in profile["partial"]
        assert _derive_slug("Root B") in profile["partial"]


# ===========================================================================
# bkt_states_from_profile
# ===========================================================================

class TestBktStatesFromProfile:

    def test_mastered_kc_maps_to_p_0_90(self, linear_map):
        kg = build_kg_from_domain_map(linear_map)
        profile = {"mastered": ["foundations"], "partial": [], "absent": ["core", "advanced"], "misconceptions": []}
        states = bkt_states_from_profile(profile, kg)
        assert states["foundations"]["p_mastered"] == pytest.approx(0.90)
        assert states["foundations"]["knowledge_class"] == "mastered"

    def test_partial_kc_maps_to_p_0_50(self, linear_map):
        kg = build_kg_from_domain_map(linear_map)
        profile = {"mastered": [], "partial": ["foundations"], "absent": ["core", "advanced"], "misconceptions": []}
        states = bkt_states_from_profile(profile, kg)
        assert states["foundations"]["p_mastered"] == pytest.approx(0.50)
        assert states["foundations"]["knowledge_class"] == "partial"

    def test_absent_kc_maps_to_p_0_10(self, linear_map):
        kg = build_kg_from_domain_map(linear_map)
        profile = {"mastered": [], "partial": [], "absent": ["foundations", "core", "advanced"], "misconceptions": []}
        states = bkt_states_from_profile(profile, kg)
        for s in states.values():
            assert s["p_mastered"] == pytest.approx(0.10)
            assert s["knowledge_class"] == "absent"

    def test_all_kcs_represented(self, linear_map):
        profile, kg = generate_profile(linear_map, preset="novice")
        states = bkt_states_from_profile(profile, kg)
        assert set(states.keys()) == {"foundations", "core", "advanced"}

    def test_observation_history_starts_empty(self, linear_map):
        profile, kg = generate_profile(linear_map, preset="expert")
        states = bkt_states_from_profile(profile, kg)
        for s in states.values():
            assert s["observation_history"] == []

    def test_generate_profile_then_bkt_states_consistent(self, linear_map):
        """Round-trip: generate_profile → bkt_states_from_profile matches profile."""
        profile, kg = generate_profile(linear_map, preset="partial_knowledge")
        states = bkt_states_from_profile(profile, kg)
        for kc_id in profile["mastered"]:
            assert states[kc_id]["p_mastered"] == pytest.approx(0.90)
        for kc_id in profile["partial"]:
            assert states[kc_id]["p_mastered"] == pytest.approx(0.50)
        for kc_id in profile["absent"]:
            assert states[kc_id]["p_mastered"] == pytest.approx(0.10)
