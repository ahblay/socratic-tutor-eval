#!/usr/bin/env python3
"""
student_evaluator.py — BKT-based mastery estimator for the student evaluation framework.

Observes dialogue turns and maintains a Bayesian Knowledge Tracing estimate of
mastery per KC node in the Junyi KC graph.

Public API
----------
BKTState                              (dataclass)
OBSERVATION_CLASSES                   (ordered list, weakest last)
classify_observations(student_message, kcs) -> list[dict]
update_bkt(state, observation_class)  -> float  (new p_mastered)
get_knowledge_frontier(bkt_states, kg) -> list[str]
evaluate_turn(student_message, bkt_states, kg) -> dict
init_bkt_states(profile, kg)          -> dict[str, BKTState]
"""

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# BKT parameters
# ---------------------------------------------------------------------------

# P(L_0) — initial probability of mastery by knowledge state
P_L0: dict[str, float] = {
    "mastered": 0.90,
    "partial":  0.50,
    "absent":   0.10,
}

P_TRANSIT   = 0.10   # P(T): probability of learning from one opportunity
P_GUESS     = 0.25   # P(G): probability of a correct response despite non-mastery
P_SLIP      = 0.10   # P(S): probability of an incorrect response despite mastery

# Observation classes ordered from strongest positive evidence to strongest negative
OBSERVATION_CLASSES = [
    "strong_articulation",  # explains KC correctly in own words, unprompted
    "weak_articulation",    # correct but minimal / fragmented
    "guided_recognition",   # correct only after visible scaffolding from teacher
    "absent",               # KC not engaged this turn
    "misconception",        # student states a wrong belief about this KC
    "contradiction",        # student contradicts their own prior response on this KC
]

# Map observation class -> effective correctness weight for BKT update
# BKT standard: P(correct | mastery_state) drives the update.
# We encode observation class as a pseudo-correct probability.
_OBS_CORRECT_WEIGHT: dict[str, float] = {
    "strong_articulation": 1.0,
    "weak_articulation":   0.75,
    "guided_recognition":  0.5,
    "absent":              None,   # no update — skip
    "misconception":       0.0,
    "contradiction":       0.0,
}


# ---------------------------------------------------------------------------
# BKTState dataclass
# ---------------------------------------------------------------------------

@dataclass
class BKTState:
    kc_id: str
    p_mastered: float
    knowledge_class: str          # "mastered" | "partial" | "absent"
    observation_history: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kc_id": self.kc_id,
            "p_mastered": round(self.p_mastered, 4),
            "knowledge_class": self.knowledge_class,
            "observation_history": self.observation_history,
        }


# ---------------------------------------------------------------------------
# BKT state initializer
# ---------------------------------------------------------------------------

def init_bkt_states(profile: dict, kg: dict) -> dict[str, "BKTState"]:
    """
    Initialize BKT states for all KCs referenced in the profile
    (mastered, partial, absent) plus all KCs present in the graph.

    KCs not explicitly listed in any category default to "absent".
    """
    all_kc_ids = {kc["id"] for kc in kg.get("kcs", [])}

    mastered_set = set(profile.get("mastered", []))
    partial_set  = set(profile.get("partial", []))
    absent_set   = set(profile.get("absent", []))

    # Any KC in the graph not listed in any category defaults to absent
    unlisted = all_kc_ids - mastered_set - partial_set - absent_set

    states: dict[str, BKTState] = {}

    for kc_id in sorted(all_kc_ids | mastered_set | partial_set | absent_set):
        if kc_id in mastered_set:
            klass = "mastered"
        elif kc_id in partial_set:
            klass = "partial"
        else:
            klass = "absent"

        states[kc_id] = BKTState(
            kc_id=kc_id,
            p_mastered=P_L0[klass],
            knowledge_class=klass,
        )

    return states


# ---------------------------------------------------------------------------
# BKT update rule
# ---------------------------------------------------------------------------

def update_bkt(state: BKTState, observation_class: str) -> float:
    """
    Apply one BKT update step given an observation class.

    Standard BKT (Corbett & Anderson 1994):
      P(L_n | correct) = P(L_{n-1}) * (1 - P_SLIP) /
                         [P(L_{n-1}) * (1 - P_SLIP) + (1 - P(L_{n-1})) * P_GUESS]

      P(L_n | incorrect) = P(L_{n-1}) * P_SLIP /
                           [P(L_{n-1}) * P_SLIP + (1 - P(L_{n-1})) * (1 - P_GUESS)]

    Then apply transition:
      P(L_n+1) = P(L_n|obs) + (1 - P(L_n|obs)) * P_TRANSIT

    For our multi-class observations we interpolate between correct and
    incorrect using _OBS_CORRECT_WEIGHT.
    """
    weight = _OBS_CORRECT_WEIGHT.get(observation_class)
    if weight is None:
        # "absent" — no evidence, but still apply a small transition
        p_post = state.p_mastered + (1 - state.p_mastered) * P_TRANSIT * 0.1
        state.p_mastered = min(p_post, 0.999)
        state.observation_history.append(observation_class)
        return state.p_mastered

    p = state.p_mastered

    # Weighted interpolation between fully-correct and fully-incorrect update
    p_correct   = p * (1 - P_SLIP) / (p * (1 - P_SLIP) + (1 - p) * P_GUESS)
    p_incorrect = p * P_SLIP       / (p * P_SLIP       + (1 - p) * (1 - P_GUESS))

    p_post = weight * p_correct + (1 - weight) * p_incorrect

    # Transition (learning opportunity)
    p_post = p_post + (1 - p_post) * P_TRANSIT

    # Clamp to valid probability range
    p_post = max(0.001, min(0.999, p_post))

    state.p_mastered = p_post
    state.observation_history.append(observation_class)
    return p_post


# ---------------------------------------------------------------------------
# Observation classifier (LLM call)
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """\
You are an educational assessment system. A student just produced the message below
during a tutoring session. Classify which knowledge components (KCs) the student
engages with and at what observation level.

AVAILABLE KCs (id: name):
{kc_listing}

OBSERVATION CLASSES (choose exactly one per KC that is engaged):
- strong_articulation  : student explains the KC correctly in their own words, unprompted
- weak_articulation    : student gives a correct but minimal or fragmented response about the KC
- guided_recognition   : student gets it right only after obvious scaffolding visible in the message
- absent               : KC is not engaged this turn (do NOT include in output)
- misconception        : student states a factually wrong belief about the KC
- contradiction        : student contradicts something they said earlier about this KC

STUDENT MESSAGE:
{student_message}

Return a JSON array of KC engagements. Only include KCs actually engaged (not "absent").
Most turns engage 0-2 KCs. If nothing is engaged, return an empty array.

[
  {{
    "kc_id": "<one of the KC IDs above>",
    "observation_class": "<one of the classes above, not absent>",
    "evidence_quote": "<exact quote from the student message that supports this classification>"
  }}
]

Respond with the JSON array only — no other text."""


def classify_observations(student_message: str, kcs: list[dict]) -> list[dict]:
    """
    Use the Claude CLI to classify which KCs the student's message engages with
    and at what observation level.

    Parameters
    ----------
    student_message : the student's turn text (self-assessment block stripped)
    kcs             : list of KC dicts from the KG {"id": ..., "name": ...}

    Returns
    -------
    list of {"kc_id": str, "observation_class": str, "evidence_quote": str}
    """
    kc_listing = "\n".join(f"  {kc['id']}: {kc['name']}" for kc in kcs)
    prompt = _CLASSIFY_PROMPT.format(
        kc_listing=kc_listing,
        student_message=student_message[:2000],  # safety truncation
    )

    try:
        print(f"  [classifier] prompt ~{len(prompt)//4} tokens", file=sys.stderr)
        result = subprocess.run(
            ["claude", "-p", "--no-session-persistence", "--output-format", "text",
             "--model", "claude-haiku-4-5-20251001", prompt],
            capture_output=True,
            text=True,
            timeout=45,
        )
        raw = result.stdout.strip().strip("```json").strip("```").strip()
    except Exception as e:
        print(f"  Warning: observation classifier failed ({e})", file=sys.stderr)
        return []

    try:
        observations = json.loads(raw)
        # Validate structure
        validated = []
        valid_classes = set(OBSERVATION_CLASSES) - {"absent"}
        for obs in observations:
            if (
                isinstance(obs, dict)
                and obs.get("kc_id")
                and obs.get("observation_class") in valid_classes
            ):
                validated.append({
                    "kc_id": obs["kc_id"],
                    "observation_class": obs["observation_class"],
                    "evidence_quote": obs.get("evidence_quote", ""),
                })
        return validated
    except (json.JSONDecodeError, TypeError) as e:
        print(f"  Warning: could not parse observation classifications ({e})",
              file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Knowledge frontier
# ---------------------------------------------------------------------------

def get_knowledge_frontier(
    bkt_states: dict[str, "BKTState"],
    kg: dict,
    target_kcs: list[str] | None = None,
) -> list[str]:
    """
    Return KC IDs that are on the student's knowledge frontier:
    - All direct prerequisite KCs have p_mastered > 0.7 (prerequisites met)
    - The KC itself has p_mastered < 0.7 (not yet firmly mastered)

    If target_kcs is provided, results are restricted to that set so the
    frontier reflects only the session's learning goals, not the full graph.
    """
    # Build prerequisite map: kc_id -> set of KCs that must be mastered first
    prerequisites: dict[str, set[str]] = {kc_id: set() for kc_id in bkt_states}
    for edge in kg.get("edges", []):
        to_kc = edge.get("to")
        from_kc = edge.get("from")
        if to_kc in prerequisites:
            prerequisites[to_kc].add(from_kc)

    candidates = set(target_kcs) if target_kcs else set(bkt_states.keys())

    frontier = []
    for kc_id, state in bkt_states.items():
        if kc_id not in candidates:
            continue
        if state.p_mastered >= 0.7:
            continue  # already mastered
        prereqs = prerequisites.get(kc_id, set())
        if all(
            bkt_states[p].p_mastered > 0.7
            for p in prereqs
            if p in bkt_states
        ):
            frontier.append(kc_id)

    return sorted(frontier)


# ---------------------------------------------------------------------------
# Estimated phase
# ---------------------------------------------------------------------------

def _estimate_phase(
    bkt_states: dict[str, "BKTState"],
    target_kcs: list[str],
) -> int:
    """
    Return the index (0-based) into the target_kcs list of the furthest KC
    that has been mastered (p_mastered > 0.7).

    Returns -1 if no target KC is mastered yet.
    """
    last_mastered = -1
    for i, kc_id in enumerate(target_kcs):
        if kc_id in bkt_states and bkt_states[kc_id].p_mastered > 0.7:
            last_mastered = i
    return last_mastered


# ---------------------------------------------------------------------------
# Full turn evaluator
# ---------------------------------------------------------------------------

def evaluate_turn(
    student_message: str,
    bkt_states: dict[str, "BKTState"],
    kg: dict,
    target_kcs: list[str] | None = None,
) -> dict:
    """
    Run classify_observations, update BKT states, compute knowledge frontier.

    Parameters
    ----------
    student_message : cleaned student message (self-assessment block removed)
    bkt_states      : current BKT state dict (mutated in place)
    kg              : full Junyi KC graph dict
    target_kcs      : ordered list of target KC IDs (for phase estimation)

    Returns
    -------
    {
      "observations":       list of observation dicts,
      "updated_bkt":        {kc_id: p_mastered, ...},
      "knowledge_frontier": [kc_id, ...],
      "estimated_phase":    int,
    }
    """
    kcs = kg.get("kcs", [])

    # Classify what the student engaged with this turn
    observations = classify_observations(student_message, kcs)

    # Apply BKT updates for each observed KC
    for obs in observations:
        kc_id = obs["kc_id"]
        obs_class = obs["observation_class"]
        if kc_id in bkt_states:
            update_bkt(bkt_states[kc_id], obs_class)
        else:
            # KC observed but not in our state dict — add it as absent-initialized
            new_state = BKTState(
                kc_id=kc_id,
                p_mastered=P_L0["absent"],
                knowledge_class="absent",
            )
            update_bkt(new_state, obs_class)
            bkt_states[kc_id] = new_state

    # Apply a tiny no-engagement update for all other KCs (time passing)
    observed_kcs = {obs["kc_id"] for obs in observations}
    for kc_id, state in bkt_states.items():
        if kc_id not in observed_kcs:
            # "absent" observation — minimal drift toward learning
            update_bkt(state, "absent")

    # Compute frontier and phase (scoped to target KCs)
    frontier = get_knowledge_frontier(bkt_states, kg, target_kcs=target_kcs)
    phase = _estimate_phase(bkt_states, target_kcs or [])

    return {
        "observations": observations,
        "updated_bkt": {
            kc_id: round(state.p_mastered, 4)
            for kc_id, state in bkt_states.items()
        },
        "knowledge_frontier": frontier,
        "estimated_phase": phase,
    }


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import yaml

    kg_path      = Path(__file__).parent / "data" / "junyi_kg.json"
    profile_path = Path(__file__).parent / "students.yaml"

    with open(kg_path) as f:
        kg = json.load(f)
    with open(profile_path) as f:
        profiles_data = yaml.safe_load(f)

    profile = profiles_data["profiles"][1]  # partial_knowledge
    states = init_bkt_states(profile, kg)
    target_kcs = profile.get("target_kcs", [])

    print(f"Initialized {len(states)} BKT states for profile: {profile['name']}")
    for kc_id in target_kcs:
        st = states.get(kc_id)
        if st:
            print(f"  {kc_id}: p_mastered={st.p_mastered:.2f} ({st.knowledge_class})")

    frontier = get_knowledge_frontier(states, kg, target_kcs=target_kcs)
    print(f"Knowledge frontier: {frontier}")

    frontier = get_knowledge_frontier(states, kg)
    print(f"\nInitial knowledge frontier: {frontier}")
