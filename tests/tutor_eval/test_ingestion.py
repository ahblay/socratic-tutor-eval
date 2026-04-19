"""
tests/tutor_eval/test_ingestion.py

Unit tests for the raw-transcript ingestion pipeline.
No API calls — covers only pure-Python functions:
  - tutor_eval/ingestion/schema.py      (validate_raw_transcript)
  - tutor_eval/ingestion/converter.py   (_derive_slug, _make_bkt_states,
                                         prepare_analysis_input)
  - tutor_eval/ingestion/domain_resolver.py  (normalize_domain_map, _is_enriched)
"""

import pytest
from tutor_eval.ingestion.schema import validate_raw_transcript
from tutor_eval.ingestion.converter import (
    _derive_slug,
    _make_bkt_states,
    prepare_analysis_input,
)
from tutor_eval.ingestion.domain_resolver import normalize_domain_map, _is_enriched


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def full_turns():
    """16 alternating turns: 8 student + 8 tutor — satisfies min-turn check."""
    turns = []
    for _ in range(8):
        turns.append({"role": "student", "content": "I see."})
        turns.append({"role": "tutor",   "content": "What do you think?"})
    return turns


@pytest.fixture
def minimal_domain_map():
    return {
        "topic": "Nash equilibrium",
        "core_concepts": [
            {
                "concept": "Strategy",
                "prerequisite_for": ["Nash equilibrium"],
                "knowledge_type": "concept",
            },
            {
                "concept": "Nash equilibrium",
                "prerequisite_for": [],
                "knowledge_type": "concept",
            },
        ],
        "recommended_sequence": ["Strategy", "Nash equilibrium"],
    }


# ===========================================================================
# validate_raw_transcript
# ===========================================================================

class TestValidateRawTranscript:

    def test_valid_full_session_no_errors(self, full_turns):
        errors, _ = validate_raw_transcript({"topic": "Nash equilibrium", "turns": full_turns})
        assert not errors

    def test_missing_topic_is_error(self, full_turns):
        errors, _ = validate_raw_transcript({"turns": full_turns})
        assert any("topic" in e for e in errors)

    def test_missing_turns_is_error(self):
        errors, _ = validate_raw_transcript({"topic": "Nash equilibrium"})
        assert any("turns" in e for e in errors)

    def test_empty_turns_is_error(self):
        errors, _ = validate_raw_transcript({"topic": "Nash equilibrium", "turns": []})
        assert any("turns" in e for e in errors)

    def test_invalid_role_is_error(self):
        data = {
            "topic": "Nash equilibrium",
            "turns": [{"role": "teacher", "content": "Hello"}],
        }
        errors, _ = validate_raw_transcript(data)
        assert any("role" in e for e in errors)

    def test_empty_content_is_error(self):
        data = {
            "topic": "Nash equilibrium",
            "turns": [{"role": "tutor", "content": ""}],
        }
        errors, _ = validate_raw_transcript(data)
        assert any("content" in e for e in errors)

    def test_non_dict_turn_is_error(self):
        data = {"topic": "Nash equilibrium", "turns": ["not a dict"]}
        errors, _ = validate_raw_transcript(data)
        assert errors

    def test_non_dict_input_is_error(self):
        errors, _ = validate_raw_transcript("not a dict")
        assert errors

    def test_short_session_warns(self):
        data = {
            "topic": "Nash equilibrium",
            "turns": [
                {"role": "student", "content": "Hi"},
                {"role": "tutor",   "content": "Hello"},
            ],
        }
        _, warnings = validate_raw_transcript(data)
        assert warnings  # < 8 tutor turns

    def test_exactly_8_tutor_turns_no_length_warning(self, full_turns):
        _, warnings = validate_raw_transcript({"topic": "Nash equilibrium", "turns": full_turns})
        assert not any("tutor turn" in w for w in warnings)

    def test_unknown_bkt_preset_warns(self, full_turns):
        data = {"topic": "Nash equilibrium", "bkt_preset": "genius", "turns": full_turns}
        _, warnings = validate_raw_transcript(data)
        assert any("bkt_preset" in w for w in warnings)

    def test_known_bkt_presets_no_warning(self, full_turns):
        for preset in ("absent", "prereqs_mastered", "all_partial"):
            data = {"topic": "Nash equilibrium", "bkt_preset": preset, "turns": full_turns}
            _, warnings = validate_raw_transcript(data)
            assert not any("bkt_preset" in w for w in warnings), f"false warning for preset {preset!r}"


# ===========================================================================
# _derive_slug (converter)
# ===========================================================================

class TestDeriveSlugConverter:

    def test_spaces_become_hyphens(self):
        assert _derive_slug("Nash equilibrium") == "nash-equilibrium"

    def test_special_chars_stripped(self):
        assert _derive_slug("Von Neumann–Morgenstern!") == "von-neumann-morgenstern"

    def test_uppercase_lowered(self):
        assert _derive_slug("UPPER CASE") == "upper-case"

    def test_truncated_at_64_chars(self):
        assert len(_derive_slug("a" * 100)) == 64

    def test_no_leading_or_trailing_hyphens(self):
        slug = _derive_slug("  --hello--  ")
        assert not slug.startswith("-")
        assert not slug.endswith("-")

    def test_numbers_preserved(self):
        assert _derive_slug("Q1 strategy") == "q1-strategy"


# ===========================================================================
# _make_bkt_states (converter)
# ===========================================================================

class TestMakeBKTStates:
    """No API calls — tests preset logic against a static domain map."""

    @pytest.fixture
    def chain_map(self):
        """Foundation → Advanced (Foundation is the root)."""
        return {
            "core_concepts": [
                {"concept": "Foundation", "prerequisite_for": ["Advanced"]},
                {"concept": "Advanced",   "prerequisite_for": []},
            ]
        }

    def test_absent_all_p_0_10(self, chain_map):
        states = _make_bkt_states(chain_map, "absent")
        assert all(s["p_mastered"] == pytest.approx(0.10) for s in states.values())
        assert all(s["knowledge_class"] == "absent" for s in states.values())

    def test_all_partial_p_0_50(self, chain_map):
        states = _make_bkt_states(chain_map, "all_partial")
        assert all(s["p_mastered"] == pytest.approx(0.50) for s in states.values())

    def test_prereqs_mastered_root_is_high(self, chain_map):
        states = _make_bkt_states(chain_map, "prereqs_mastered")
        root_id = _derive_slug("Foundation")
        leaf_id = _derive_slug("Advanced")
        assert states[root_id]["p_mastered"] == pytest.approx(0.90)
        assert states[leaf_id]["p_mastered"] == pytest.approx(0.10)

    def test_unknown_preset_defaults_to_absent(self, chain_map):
        states = _make_bkt_states(chain_map, "nonexistent_preset")
        assert all(s["p_mastered"] == pytest.approx(0.10) for s in states.values())

    def test_all_states_have_empty_observation_history(self, chain_map):
        for preset in ("absent", "all_partial", "prereqs_mastered"):
            states = _make_bkt_states(chain_map, preset)
            assert all(s["observation_history"] == [] for s in states.values())

    def test_correct_number_of_states(self, chain_map):
        states = _make_bkt_states(chain_map, "absent")
        assert len(states) == 2


# ===========================================================================
# prepare_analysis_input (converter)
# ===========================================================================

class TestPrepareAnalysisInput:

    @pytest.fixture
    def raw(self):
        return {
            "topic": "Nash equilibrium",
            "source": "gpt-4o",
            "turns": [
                {"role": "student", "content": "Hi"},
                {"role": "tutor",   "content": "What do you think?"},
            ],
        }

    def test_student_role_normalized_to_user(self, raw, minimal_domain_map):
        out = prepare_analysis_input(raw, minimal_domain_map)
        roles = {t["role"] for t in out["lesson_turns"]}
        assert "student" not in roles
        assert "user" in roles

    def test_tutor_role_preserved(self, raw, minimal_domain_map):
        out = prepare_analysis_input(raw, minimal_domain_map)
        roles = {t["role"] for t in out["lesson_turns"]}
        assert "tutor" in roles

    def test_turn_numbers_sequential_from_one(self, raw, minimal_domain_map):
        out = prepare_analysis_input(raw, minimal_domain_map)
        nums = [t["turn_number"] for t in out["lesson_turns"]]
        assert nums == list(range(1, len(out["lesson_turns"]) + 1))

    def test_session_id_generated_when_absent(self, minimal_domain_map):
        raw = {"topic": "Test", "turns": [{"role": "tutor", "content": "hi"}]}
        out = prepare_analysis_input(raw, minimal_domain_map)
        assert out["session_id"]

    def test_explicit_session_id_preserved(self, minimal_domain_map):
        raw = {
            "topic": "Test",
            "session_id": "my-session-abc",
            "turns": [{"role": "tutor", "content": "hi"}],
        }
        out = prepare_analysis_input(raw, minimal_domain_map)
        assert out["session_id"] == "my-session-abc"

    def test_article_id_from_source_field(self, raw, minimal_domain_map):
        out = prepare_analysis_input(raw, minimal_domain_map)
        assert out["article_id"] == "gpt-4o"

    def test_article_id_slugified_topic_when_no_source(self, minimal_domain_map):
        raw = {"topic": "Nash Equilibrium", "turns": [{"role": "tutor", "content": "hi"}]}
        out = prepare_analysis_input(raw, minimal_domain_map)
        assert out["article_id"] == "nash-equilibrium"

    def test_bkt_preset_drives_initial_states(self, minimal_domain_map):
        raw = {
            "topic": "Test",
            "bkt_preset": "all_partial",
            "turns": [{"role": "tutor", "content": "hi"}],
        }
        out = prepare_analysis_input(raw, minimal_domain_map)
        for s in out["bkt_initial_states"].values():
            assert s["p_mastered"] == pytest.approx(0.50)

    def test_explicit_bkt_initial_states_override_preset(self, minimal_domain_map):
        raw = {
            "topic": "Test",
            "bkt_preset": "all_partial",
            "bkt_initial_states": {
                "strategy": {"p_mastered": 0.77, "knowledge_class": "mastered", "observation_history": []}
            },
            "turns": [{"role": "tutor", "content": "hi"}],
        }
        out = prepare_analysis_input(raw, minimal_domain_map)
        assert out["bkt_initial_states"]["strategy"]["p_mastered"] == pytest.approx(0.77)

    def test_teacher_role_normalized_to_tutor(self, minimal_domain_map):
        raw = {
            "topic": "Test",
            "turns": [{"role": "teacher", "content": "What do you think?"}],
        }
        out = prepare_analysis_input(raw, minimal_domain_map)
        roles = {t["role"] for t in out["lesson_turns"]}
        assert "teacher" not in roles
        assert "tutor" in roles

    def test_assessment_turns_always_empty_list(self, raw, minimal_domain_map):
        out = prepare_analysis_input(raw, minimal_domain_map)
        assert out["assessment_turns"] == []

    def test_article_title_matches_topic(self, raw, minimal_domain_map):
        out = prepare_analysis_input(raw, minimal_domain_map)
        assert out["article_title"] == "Nash equilibrium"


# ===========================================================================
# normalize_domain_map
# ===========================================================================

class TestNormalizeDomainMap:

    def test_webapp_format_concepts_preserved(self):
        dm = {
            "core_concepts": [
                {"concept": "A", "prerequisite_for": ["B"], "knowledge_type": "concept"},
                {"concept": "B", "prerequisite_for": [], "knowledge_type": "convention"},
            ],
            "recommended_sequence": ["A", "B"],
        }
        result = normalize_domain_map(dm)
        assert len(result["core_concepts"]) == 2
        assert result["core_concepts"][0]["concept"] == "A"

    def test_name_field_accepted_instead_of_concept(self):
        dm = {"core_concepts": [{"name": "Backward induction", "prerequisite_for": []}]}
        result = normalize_domain_map(dm)
        assert result["core_concepts"][0]["concept"] == "Backward induction"

    def test_title_field_accepted(self):
        dm = {"core_concepts": [{"title": "Subgame perfect", "prerequisite_for": []}]}
        result = normalize_domain_map(dm)
        assert result["core_concepts"][0]["concept"] == "Subgame perfect"

    def test_invalid_knowledge_type_defaults_to_concept(self):
        dm = {"core_concepts": [{"concept": "X", "knowledge_type": "junk"}]}
        result = normalize_domain_map(dm)
        assert result["core_concepts"][0]["knowledge_type"] == "concept"

    def test_missing_recommended_sequence_filled_from_concepts(self):
        dm = {
            "core_concepts": [
                {"concept": "A", "prerequisite_for": []},
                {"concept": "B", "prerequisite_for": []},
            ]
        }
        result = normalize_domain_map(dm)
        assert result["recommended_sequence"] == ["A", "B"]

    def test_string_concept_entry_converted(self):
        dm = {"core_concepts": ["Just a string"]}
        result = normalize_domain_map(dm)
        assert result["core_concepts"][0]["concept"] == "Just a string"

    def test_phase_topics_flattened_to_core_concepts(self):
        dm = {
            "phase_topics": {
                "Q1": {"core_concepts": [{"concept": "Alpha", "prerequisite_for": []}]},
                "Q2": {"core_concepts": [{"concept": "Beta",  "prerequisite_for": []}]},
            }
        }
        result = normalize_domain_map(dm)
        names = {c["concept"] for c in result["core_concepts"]}
        assert names == {"Alpha", "Beta"}

    def test_phase_topics_deduplicates_same_concept(self):
        dm = {
            "phase_topics": {
                "Q1": {"core_concepts": [{"concept": "Alpha", "prerequisite_for": []}]},
                "Q2": {"core_concepts": [{"concept": "Alpha", "prerequisite_for": []}]},
            }
        }
        result = normalize_domain_map(dm)
        assert len(result["core_concepts"]) == 1

    def test_kg_format_edges_become_prerequisite_for(self):
        dm = {
            "kcs": [{"id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}],
            "edges": [{"from": "a", "to": "b"}],
        }
        result = normalize_domain_map(dm)
        assert len(result["core_concepts"]) == 2
        alpha = next(c for c in result["core_concepts"] if c["concept"] == "Alpha")
        assert "Beta" in alpha["prerequisite_for"]

    def test_flat_string_list_under_concepts_key(self):
        dm = {"concepts": ["Alpha", "Beta", "Gamma"]}
        result = normalize_domain_map(dm)
        assert len(result["core_concepts"]) == 3
        assert all(c["knowledge_type"] == "concept" for c in result["core_concepts"])

    def test_flat_string_list_under_topics_key(self):
        dm = {"topics": ["Alpha", "Beta"]}
        result = normalize_domain_map(dm)
        assert len(result["core_concepts"]) == 2


# ===========================================================================
# _is_enriched
# ===========================================================================

class TestIsEnriched:

    def test_true_when_any_concept_has_knowledge_type(self):
        dm = {"core_concepts": [{"concept": "X", "knowledge_type": "concept"}]}
        assert _is_enriched(dm) is True

    def test_true_for_any_valid_knowledge_type(self):
        for kt in ("concept", "convention", "narrative"):
            dm = {"core_concepts": [{"concept": "X", "knowledge_type": kt}]}
            assert _is_enriched(dm) is True, f"Expected True for knowledge_type={kt!r}"

    def test_false_when_no_knowledge_type(self):
        dm = {"core_concepts": [{"concept": "X"}]}
        assert _is_enriched(dm) is False

    def test_false_empty_concepts(self):
        assert _is_enriched({"core_concepts": []}) is False

    def test_false_no_core_concepts_key(self):
        assert _is_enriched({}) is False

    def test_true_if_only_one_concept_has_knowledge_type(self):
        dm = {
            "core_concepts": [
                {"concept": "A"},
                {"concept": "B", "knowledge_type": "narrative"},
            ]
        }
        assert _is_enriched(dm) is True
