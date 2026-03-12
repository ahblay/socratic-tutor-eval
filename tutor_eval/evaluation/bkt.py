"""
tutor_eval/evaluation/bkt.py

SDK-based port of student_evaluator.py with KC filtering.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field

import anthropic


# ---------------------------------------------------------------------------
# BKT parameters  (unchanged from student_evaluator.py)
# ---------------------------------------------------------------------------

P_L0: dict[str, float] = {
    "mastered": 0.90,
    "partial":  0.50,
    "absent":   0.10,
}

P_TRANSIT = 0.10
P_GUESS   = 0.25
P_SLIP    = 0.10

OBSERVATION_CLASSES = [
    "strong_articulation",
    "weak_articulation",
    "guided_recognition",
    "absent",
    "misconception",
    "contradiction",
]

_OBS_CORRECT_WEIGHT: dict[str, float | None] = {
    "strong_articulation": 1.0,
    "weak_articulation":   0.75,
    "guided_recognition":  0.5,
    "absent":              None,
    "misconception":       0.0,
    "contradiction":       0.0,
}


# ---------------------------------------------------------------------------
# BKTState dataclass  (unchanged)
# ---------------------------------------------------------------------------

@dataclass
class BKTState:
    kc_id: str
    p_mastered: float
    knowledge_class: str
    observation_history: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kc_id": self.kc_id,
            "p_mastered": round(self.p_mastered, 4),
            "knowledge_class": self.knowledge_class,
            "observation_history": self.observation_history,
        }


# ---------------------------------------------------------------------------
# BKT state initializer  (unchanged)
# ---------------------------------------------------------------------------

def init_bkt_states(profile: dict, kg: dict) -> dict[str, BKTState]:
    all_kc_ids = {kc["id"] for kc in kg.get("kcs", [])}

    mastered_set = set(profile.get("mastered", []))
    partial_set  = set(profile.get("partial", []))
    absent_set   = set(profile.get("absent", []))

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
# BKT update rule  (unchanged)
# ---------------------------------------------------------------------------

def update_bkt(state: BKTState, observation_class: str) -> float:
    weight = _OBS_CORRECT_WEIGHT.get(observation_class)
    if weight is None:
        p_post = state.p_mastered + (1 - state.p_mastered) * P_TRANSIT * 0.1
        state.p_mastered = min(p_post, 0.999)
        state.observation_history.append(observation_class)
        return state.p_mastered

    p = state.p_mastered

    p_correct   = p * (1 - P_SLIP) / (p * (1 - P_SLIP) + (1 - p) * P_GUESS)
    p_incorrect = p * P_SLIP       / (p * P_SLIP       + (1 - p) * (1 - P_GUESS))

    p_post = weight * p_correct + (1 - weight) * p_incorrect
    p_post = p_post + (1 - p_post) * P_TRANSIT
    p_post = max(0.001, min(0.999, p_post))

    state.p_mastered = p_post
    state.observation_history.append(observation_class)
    return p_post


# ---------------------------------------------------------------------------
# KC filtering helper
# ---------------------------------------------------------------------------

def _get_relevant_kcs(
    kg: dict,
    profile: dict,
    target_kcs: list[str],
    frontier: list[str],
) -> list[dict]:
    """
    Return a filtered list of KC dicts relevant to the current session.

    Includes:
    - All target_kcs
    - Direct prerequisites of target_kcs (one hop: edges where `to` is in
      target_kcs; include the `from` KC)
    - Any KCs listed in profile["misconceptions"] (the "kc" field)
    - Currently active frontier KCs (subset of target_kcs, already covered)
    """
    target_set    = set(target_kcs)
    frontier_set  = set(frontier)
    misconception_kcs = {m.get("kc") for m in profile.get("misconceptions", []) if m.get("kc")}

    # One-hop prerequisites
    prereq_set: set[str] = set()
    for edge in kg.get("edges", []):
        if edge.get("to") in target_set:
            prereq_set.add(edge.get("from"))

    relevant_ids = target_set | frontier_set | misconception_kcs | prereq_set

    all_kcs = {kc["id"]: kc for kc in kg.get("kcs", [])}
    return [all_kcs[kc_id] for kc_id in relevant_ids if kc_id in all_kcs]


# ---------------------------------------------------------------------------
# Observation classifier prompt
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


# ---------------------------------------------------------------------------
# Observation classifier (SDK)
# ---------------------------------------------------------------------------

def classify_observations(
    student_message: str,
    kcs: list[dict],
    client: anthropic.Anthropic,
    profile: dict | None = None,
    target_kcs: list[str] | None = None,
    frontier: list[str] | None = None,
    kg: dict | None = None,
    verbose: bool = False,
) -> list[dict]:
    """
    Classify which KCs the student's message engages with, using a filtered KC list.
    """
    # Apply KC filtering if possible
    if kg is not None and profile is not None and target_kcs is not None:
        relevant_kcs = _get_relevant_kcs(
            kg, profile, target_kcs, frontier or []
        )
        if relevant_kcs:
            kcs = relevant_kcs

    kc_listing = "\n".join(f"  {kc['id']}: {kc['name']}" for kc in kcs)
    prompt = _CLASSIFY_PROMPT.format(
        kc_listing=kc_listing,
        student_message=student_message[:2000],
    )

    try:
        if verbose:
            print(
                f"  [classifier] {len(kcs)} KCs, prompt ~{len(prompt)//4} tokens",
                file=sys.stderr,
            )
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    except Exception as e:
        print(f"  Warning: observation classifier failed ({e})", file=sys.stderr)
        return []

    try:
        observations = json.loads(raw)
        validated = []
        valid_classes = set(OBSERVATION_CLASSES) - {"absent"}
        for obs in observations:
            if (
                isinstance(obs, dict)
                and obs.get("kc_id")
                and obs.get("observation_class") in valid_classes
            ):
                validated.append(
                    {
                        "kc_id": obs["kc_id"],
                        "observation_class": obs["observation_class"],
                        "evidence_quote": obs.get("evidence_quote", ""),
                    }
                )
        return validated
    except (json.JSONDecodeError, TypeError) as e:
        print(
            f"  Warning: could not parse observation classifications ({e})",
            file=sys.stderr,
        )
        return []


# ---------------------------------------------------------------------------
# Knowledge frontier  (unchanged)
# ---------------------------------------------------------------------------

def get_knowledge_frontier(
    bkt_states: dict[str, BKTState],
    kg: dict,
    target_kcs: list[str] | None = None,
) -> list[str]:
    prerequisites: dict[str, set[str]] = {kc_id: set() for kc_id in bkt_states}
    for edge in kg.get("edges", []):
        to_kc   = edge.get("to")
        from_kc = edge.get("from")
        if to_kc in prerequisites:
            prerequisites[to_kc].add(from_kc)

    candidates = set(target_kcs) if target_kcs else set(bkt_states.keys())

    frontier = []
    for kc_id, state in bkt_states.items():
        if kc_id not in candidates:
            continue
        if state.p_mastered >= 0.7:
            continue
        prereqs = prerequisites.get(kc_id, set())
        if all(
            bkt_states[p].p_mastered > 0.7
            for p in prereqs
            if p in bkt_states
        ):
            frontier.append(kc_id)

    return sorted(frontier)


# ---------------------------------------------------------------------------
# Estimated phase  (unchanged)
# ---------------------------------------------------------------------------

def _estimate_phase(
    bkt_states: dict[str, BKTState],
    target_kcs: list[str],
) -> int:
    last_mastered = -1
    for i, kc_id in enumerate(target_kcs):
        if kc_id in bkt_states and bkt_states[kc_id].p_mastered > 0.7:
            last_mastered = i
    return last_mastered


# ---------------------------------------------------------------------------
# BKTEvaluator class
# ---------------------------------------------------------------------------

class BKTEvaluator:
    def __init__(
        self,
        profile: dict | None = None,
        kg: dict | None = None,
        bkt_states: dict[str, BKTState] | None = None,
        target_kcs: list[str] | None = None,
    ) -> None:
        self.profile = profile or {}
        self.kg      = kg or {"kcs": [], "edges": []}
        self.client  = anthropic.Anthropic()

        if bkt_states is not None:
            self.bkt_states = bkt_states
            self.target_kcs = target_kcs or []
        else:
            self.bkt_states = init_bkt_states(self.profile, self.kg)
            self.target_kcs = target_kcs if target_kcs is not None else self.profile.get("target_kcs", [])

        self.verbose = False  # set to True to enable classifier diagnostics

    def evaluate_turn(self, student_message: str) -> dict:
        """
        Classify observations, update BKT states, return snapshot dict.

        Returns:
          {
            "observations":       list[dict],
            "updated_bkt":        {kc_id: p_mastered},
            "knowledge_frontier": [kc_id, ...],
            "estimated_phase":    int,
          }
        """
        # Current frontier (before this update) for KC filtering
        current_frontier = get_knowledge_frontier(
            self.bkt_states, self.kg, target_kcs=self.target_kcs
        )

        # Classify with filtered KCs
        observations = classify_observations(
            student_message,
            kcs=self.kg.get("kcs", []),
            client=self.client,
            profile=self.profile,
            target_kcs=self.target_kcs,
            frontier=current_frontier,
            kg=self.kg,
            verbose=self.verbose,
        )

        # Apply BKT updates
        for obs in observations:
            kc_id     = obs["kc_id"]
            obs_class = obs["observation_class"]
            if kc_id in self.bkt_states:
                update_bkt(self.bkt_states[kc_id], obs_class)
            else:
                new_state = BKTState(
                    kc_id=kc_id,
                    p_mastered=P_L0["absent"],
                    knowledge_class="absent",
                )
                update_bkt(new_state, obs_class)
                self.bkt_states[kc_id] = new_state

        # Tiny no-engagement update for all other KCs
        observed_kcs = {obs["kc_id"] for obs in observations}
        for kc_id, state in self.bkt_states.items():
            if kc_id not in observed_kcs:
                update_bkt(state, "absent")

        # Compute frontier and phase
        frontier = get_knowledge_frontier(
            self.bkt_states, self.kg, target_kcs=self.target_kcs
        )
        phase = _estimate_phase(self.bkt_states, self.target_kcs)

        return {
            "observations": observations,
            "updated_bkt": {
                kc_id: round(state.p_mastered, 4)
                for kc_id, state in self.bkt_states.items()
            },
            "knowledge_frontier": frontier,
            "estimated_phase": phase,
        }
