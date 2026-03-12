"""
tests/tutor_eval/test_bkt.py

Unit tests for BKT math, state initialization, KC filtering,
and BKTEvaluator constructor paths.  No API calls made.
"""

import pytest
from tutor_eval.evaluation.bkt import (
    BKTEvaluator,
    BKTState,
    P_GUESS,
    P_L0,
    P_SLIP,
    P_TRANSIT,
    _get_relevant_kcs,
    get_knowledge_frontier,
    init_bkt_states,
    update_bkt,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_kg():
    """Linear chain: A → B → C"""
    return {
        "kcs": [
            {"id": "a", "name": "A"},
            {"id": "b", "name": "B"},
            {"id": "c", "name": "C"},
        ],
        "edges": [
            {"from": "a", "to": "b"},
            {"from": "b", "to": "c"},
        ],
    }


@pytest.fixture
def simple_profile():
    return {
        "target_kcs": ["a", "b", "c"],
        "mastered": [],
        "partial": [],
        "absent": [],
        "misconceptions": [],
    }


# ---------------------------------------------------------------------------
# P_L0 initialization
# ---------------------------------------------------------------------------

class TestInitBKTStates:
    def test_absent_default(self, simple_kg, simple_profile):
        states = init_bkt_states(simple_profile, simple_kg)
        for kc_id in ["a", "b", "c"]:
            assert states[kc_id].p_mastered == P_L0["absent"]
            assert states[kc_id].knowledge_class == "absent"

    def test_mastered_kcs(self, simple_kg):
        profile = {"mastered": ["a"], "partial": [], "absent": [], "misconceptions": []}
        states = init_bkt_states(profile, simple_kg)
        assert states["a"].p_mastered == P_L0["mastered"]
        assert states["b"].p_mastered == P_L0["absent"]

    def test_partial_kcs(self, simple_kg):
        profile = {"mastered": [], "partial": ["b"], "absent": [], "misconceptions": []}
        states = init_bkt_states(profile, simple_kg)
        assert states["b"].p_mastered == P_L0["partial"]

    def test_all_kcs_present(self, simple_kg, simple_profile):
        states = init_bkt_states(simple_profile, simple_kg)
        assert set(states.keys()) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# BKT update rule
# ---------------------------------------------------------------------------

class TestUpdateBKT:
    def test_strong_articulation_increases_mastery(self):
        state = BKTState("a", 0.1, "absent")
        new_p = update_bkt(state, "strong_articulation")
        assert new_p > 0.1

    def test_misconception_decreases_mastery(self):
        state = BKTState("a", 0.8, "mastered")
        new_p = update_bkt(state, "misconception")
        assert new_p < 0.8

    def test_absent_observation_nudges_slightly(self):
        state = BKTState("a", 0.1, "absent")
        p_before = state.p_mastered
        update_bkt(state, "absent")
        assert state.p_mastered > p_before  # tiny transit nudge
        assert state.p_mastered < 0.15      # but barely

    def test_observation_appended_to_history(self):
        state = BKTState("a", 0.5, "partial")
        update_bkt(state, "weak_articulation")
        assert "weak_articulation" in state.observation_history

    def test_p_mastered_clamped(self):
        state = BKTState("a", 0.999, "mastered")
        for _ in range(10):
            update_bkt(state, "strong_articulation")
        assert state.p_mastered <= 0.999

    def test_p_mastered_floor(self):
        state = BKTState("a", 0.001, "absent")
        for _ in range(10):
            update_bkt(state, "misconception")
        assert state.p_mastered >= 0.001

    def test_weight_interpolation_guided_recognition(self):
        """guided_recognition (weight=0.5) should be between strong and misconception."""
        state_strong = BKTState("a", 0.3, "absent")
        state_guided = BKTState("a", 0.3, "absent")
        state_misc   = BKTState("a", 0.3, "absent")
        update_bkt(state_strong, "strong_articulation")
        update_bkt(state_guided, "guided_recognition")
        update_bkt(state_misc,   "misconception")
        assert state_misc.p_mastered < state_guided.p_mastered < state_strong.p_mastered


# ---------------------------------------------------------------------------
# Knowledge frontier
# ---------------------------------------------------------------------------

class TestKnowledgeFrontier:
    def test_unmastered_root_is_on_frontier(self, simple_kg):
        states = {
            "a": BKTState("a", 0.1, "absent"),
            "b": BKTState("b", 0.1, "absent"),
            "c": BKTState("c", 0.1, "absent"),
        }
        frontier = get_knowledge_frontier(states, simple_kg, target_kcs=["a", "b", "c"])
        assert "a" in frontier
        assert "b" not in frontier   # prereq (a) not mastered
        assert "c" not in frontier

    def test_mastering_root_opens_next(self, simple_kg):
        states = {
            "a": BKTState("a", 0.9, "mastered"),
            "b": BKTState("b", 0.1, "absent"),
            "c": BKTState("c", 0.1, "absent"),
        }
        frontier = get_knowledge_frontier(states, simple_kg, target_kcs=["a", "b", "c"])
        assert "a" not in frontier   # already mastered (p >= 0.7)
        assert "b" in frontier
        assert "c" not in frontier

    def test_all_mastered_empty_frontier(self, simple_kg):
        states = {kc: BKTState(kc, 0.9, "mastered") for kc in ["a", "b", "c"]}
        frontier = get_knowledge_frontier(states, simple_kg, target_kcs=["a", "b", "c"])
        assert frontier == []

    def test_no_edges_all_on_frontier(self):
        kg = {"kcs": [{"id": "x"}, {"id": "y"}], "edges": []}
        states = {
            "x": BKTState("x", 0.1, "absent"),
            "y": BKTState("y", 0.1, "absent"),
        }
        frontier = get_knowledge_frontier(states, kg, target_kcs=["x", "y"])
        assert set(frontier) == {"x", "y"}


# ---------------------------------------------------------------------------
# KC filtering
# ---------------------------------------------------------------------------

class TestGetRelevantKcs:
    def test_returns_target_and_prereqs(self, simple_kg):
        profile = {"misconceptions": []}
        # target = [b, c]; prereq of b = a, prereq of c = b
        relevant = _get_relevant_kcs(simple_kg, profile, target_kcs=["b", "c"], frontier=[])
        ids = {kc["id"] for kc in relevant}
        assert "a" in ids   # prereq of b
        assert "b" in ids
        assert "c" in ids

    def test_misconception_kcs_included(self, simple_kg):
        profile = {"misconceptions": [{"kc": "a"}]}
        relevant = _get_relevant_kcs(simple_kg, profile, target_kcs=["c"], frontier=[])
        ids = {kc["id"] for kc in relevant}
        assert "a" in ids

    def test_empty_target_returns_empty(self, simple_kg):
        profile = {"misconceptions": []}
        relevant = _get_relevant_kcs(simple_kg, profile, target_kcs=[], frontier=[])
        assert relevant == []


# ---------------------------------------------------------------------------
# BKTEvaluator constructor paths
# ---------------------------------------------------------------------------

class TestBKTEvaluatorConstructor:
    def test_profile_kg_path(self, simple_kg, simple_profile):
        ev = BKTEvaluator(profile=simple_profile, kg=simple_kg)
        assert set(ev.bkt_states.keys()) == {"a", "b", "c"}
        assert ev.target_kcs == ["a", "b", "c"]

    def test_bkt_states_injection_path(self):
        """Server path: inject pre-loaded BKT states directly."""
        states = {
            "a": BKTState("a", 0.7, "mastered"),
            "b": BKTState("b", 0.2, "absent"),
        }
        ev = BKTEvaluator(bkt_states=states, target_kcs=["a", "b"])
        assert ev.bkt_states["a"].p_mastered == 0.7
        assert ev.target_kcs == ["a", "b"]

    def test_empty_constructor_defaults(self):
        """Both paths optional — should not crash."""
        ev = BKTEvaluator()
        assert ev.bkt_states == {}
        assert ev.target_kcs == []

    def test_target_kcs_override(self, simple_kg, simple_profile):
        """Explicit target_kcs overrides profile.target_kcs."""
        ev = BKTEvaluator(profile=simple_profile, kg=simple_kg, target_kcs=["a"])
        assert ev.target_kcs == ["a"]

    def test_verbose_default_false(self, simple_kg, simple_profile):
        ev = BKTEvaluator(profile=simple_profile, kg=simple_kg)
        assert ev.verbose is False
