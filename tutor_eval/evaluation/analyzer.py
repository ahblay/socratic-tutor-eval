"""
tutor_eval/evaluation/analyzer.py

Post-hoc transcript analysis.

Entry point: analyze_transcript()
Data structures: TurnResult, EvaluationResult
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

import anthropic

from tutor_eval.evaluation.bkt import (
    BKTState,
    P_L0,
    _create_with_retry,
    classify_observations,
    get_knowledge_frontier,
    update_bkt,
)


# ---------------------------------------------------------------------------
# TurnResult — per tutor turn
# ---------------------------------------------------------------------------

@dataclass
class TurnResult:
    """
    Evaluation data for a single tutor turn.

    bkt_snapshot reflects the student's knowledge AFTER the preceding student
    turn — i.e., the state the tutor was responding to.

    preceding_observations holds BKT evaluator output from the student turn
    immediately before this tutor turn.  Used by MRQ and PR misconception
    exemption.
    """

    turn_number: int

    # --- KC targeting (shared by KFT and PR) ---
    targeted_kc_id: str | None = None
    # "on_frontier" | "mastered" | "prereqs_not_met" | "off_map"
    kc_status: str | None = None

    # --- NAC ---
    # "compliant" | "violation" | "disabled"
    # "disabled" when compute_nac=False globally; excluded from NAC denominator.
    nac_verdict: str | None = None
    # Raw reviewer verdict stored for reference ("pass"/"warn"/"fail"/"disabled"/None)
    reviewer_verdict: str | None = None

    # --- LCQ ---
    # "concept" | "convention" | "narrative"
    observed_type: str | None = None   # classifier output from tutor response text
    warranted_type: str | None = None  # classifier output given KC + bkt_snapshot

    # --- MRQ ---
    # "probed" | "ignored" | None  (None = no misconception in preceding student turn)
    mrq_verdict: str | None = None

    # --- BKT state when this tutor turn was generated ---
    # Maps kc_id → p_mastered
    bkt_snapshot: dict[str, float] = field(default_factory=dict)

    # --- BKT observations from the preceding student turn ---
    # List of {"kc_id": str, "observation_class": str, "evidence_quote": str}
    preceding_observations: list[dict[str, Any]] = field(default_factory=list)

    # --- PR stall tracking (computed in pure Python, no LLM) ---
    is_stall_turn: bool = False
    # "shape1" = mastered KC drilled | "shape2" = frontier KC with no BKT progress
    stall_shape: str | None = None


# ---------------------------------------------------------------------------
# EvaluationResult — session level
# ---------------------------------------------------------------------------

@dataclass
class EvaluationResult:
    """
    Complete post-hoc evaluation for a single session.

    Stored as JSON in sessions.analysis.
    Serialize with to_dict() before writing.
    """

    session_id: str
    article_id: str

    # Per-turn data — one entry per tutor turn, in session order
    turn_results: list[TurnResult] = field(default_factory=list)

    # --- Metric scores ---
    # NAC: 1.0 when compute_nac=False
    nac: float = 1.0
    kft: float = 0.0
    pr: float = 0.0
    lcq: float = 0.0
    # None when no misconceptions were detected in the session
    mrq: float | None = None
    mrq_adjustment: float = 0.0
    # None when tutor state snapshots are unavailable (non-Claude tutors)
    tba: float | None = None

    # Composite: NAC × (0.5·KFT + 0.25·PR + 0.25·LCQ + mrq_adjustment)
    composite: float = 0.0

    # --- Session validity ---
    total_tutor_turns: int = 0
    # False when total_tutor_turns < 8 — score is unreliable
    is_valid: bool = True
    invalidity_reason: str | None = None

    # --- Response reviewer activity (diagnostic) ---
    reviewer_active: bool = False
    reviewer_rewrite_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for storage in sessions.analysis."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Domain map helpers
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:64]


def _build_kg(domain_map: dict) -> dict:
    """Convert domain map to {kcs, edges} format expected by BKTEvaluator."""
    concepts = domain_map.get("core_concepts", [])
    name_to_slug = {
        c["concept"]: _slugify(c["concept"])
        for c in concepts if c.get("concept")
    }
    kcs = [
        {"id": name_to_slug[c["concept"]], "name": c["concept"]}
        for c in concepts if c.get("concept")
    ]
    edges: list[dict] = []
    for c in concepts:
        from_slug = name_to_slug.get(c.get("concept", ""))
        if not from_slug:
            continue
        for downstream in c.get("prerequisite_for", []):
            to_slug = name_to_slug.get(downstream)
            if to_slug and to_slug != from_slug:
                edges.append({"from": from_slug, "to": to_slug})
    return {"kcs": kcs, "edges": edges}


def _get_target_kcs_from_dm(domain_map: dict) -> list[str]:
    return [_slugify(name) for name in domain_map.get("recommended_sequence", [])]


def _build_kc_type_map(domain_map: dict) -> dict[str, str]:
    """Map kc_id → knowledge_type ('concept'/'convention'/'narrative')."""
    result: dict[str, str] = {}
    for c in domain_map.get("core_concepts", []):
        name = c.get("concept", "")
        if name:
            kt = c.get("knowledge_type", "concept")
            result[_slugify(name)] = kt if kt in ("concept", "convention", "narrative") else "concept"
    return result


def _init_bkt_from_raw(
    bkt_initial_states_raw: dict,
    kg: dict,
) -> dict[str, BKTState]:
    """
    Initialize BKT states from bkt_initial_states (post-assessment data).
    Fallback: all KCs at absent (p=0.10) — root KCs will be on frontier at turn 1.
    """
    if bkt_initial_states_raw:
        return {
            kc_id: BKTState(
                kc_id=kc_id,
                p_mastered=v["p_mastered"],
                knowledge_class=v.get("knowledge_class", "absent"),
                observation_history=list(v.get("observation_history", [])),
            )
            for kc_id, v in bkt_initial_states_raw.items()
        }
    return {
        kc["id"]: BKTState(
            kc_id=kc["id"],
            p_mastered=P_L0["absent"],
            knowledge_class="absent",
        )
        for kc in kg.get("kcs", [])
    }


# ---------------------------------------------------------------------------
# Haiku classifier prompt — one call per tutor turn
# ---------------------------------------------------------------------------

_TUTOR_CLASSIFIER_PROMPT = """\
You are an educational evaluation system. For the tutor turn below, return five \
classifications as a JSON object.

KNOWLEDGE COMPONENTS (id: name [type]):
{kc_listing}

CURRENT KNOWLEDGE FRONTIER (unmastered KCs whose prerequisites are met):
{frontier_listing}

STUDENT BKT STATE (p_mastered, 0–1):
{bkt_state}

{misconception_section}PRECEDING STUDENT MESSAGE:
{student_message}

TUTOR RESPONSE:
{tutor_response}

---

## Classification 1 — targeted_kc_id
Which KC does the tutor response primarily address? Return the KC id exactly as \
listed above, or null if no specific KC is targeted.

## Classification 2 — nac_verdict
Does the response violate the Socratic method?

KC-type-specific rules:
- "convention" or "narrative" KC: presenting reference material then asking about \
  it is compliant. Providing a rule/fact/framework before a question is expected.
- "concept" KC: providing information the student could reason toward is a violation.

Always a violation regardless of KC type:
- States the correct answer explicitly
- Confirms the student is correct ("Exactly!", "Yes, that's right", "You've got it")
- Corrects the student with the right answer

Always compliant:
- Open-ended questions, requests for elaboration, neutral acknowledgments
- Convention/narrative reference material delivery followed by a question

Return "compliant" or "violation".

## Classification 3 — observed_type
What behavioral type is the tutor's response?
- "concept": pure Socratic questioning — no information provided
- "convention": presents a single fact/rule, then asks student to apply it
- "narrative": provides a structured content block, then asks student to reason

## Classification 4 — warranted_type
What response type was most appropriate given the targeted KC and this specific \
student's current knowledge?

Core question: could this student, at their current level of understanding, reason \
toward or discover the key insight of this KC — or does the content need to be \
provided first?

- "concept": student can reason toward the answer through guided questions. Use \
  when the KC involves a logical relationship or derivable pattern, OR when the \
  student already has substantial mastery (p ≥ 0.5) — even for KCs that might \
  otherwise require provision.
- "convention": KC centers on an arbitrary standard, rule, or value that cannot \
  be derived by reasoning (e.g. a specific measurement, a naming convention, a \
  regulatory definition). Use only when the student has low mastery (p < 0.5) \
  and the content is genuinely non-derivable.
- "narrative": KC requires a structured conceptual framework to be presented \
  before the student can reason productively. Use only when the student has low \
  mastery (p < 0.5) and the framework is too large to elicit through questions.

Base your judgment on the student's demonstrated understanding in the preceding \
message and their BKT state — not solely on the KC's pre-assigned type label. A \
student who has shown they can approximate or reason around a KC warrants "concept" \
even if the KC is conventionally non-derivable.

## Classification 5 — mrq_verdict
{mrq_instructions}

---

Return ONLY a JSON object:
{{
  "targeted_kc_id": "<kc_id or null>",
  "nac_verdict": "compliant|violation",
  "observed_type": "concept|convention|narrative",
  "warranted_type": "concept|convention|narrative",
  "mrq_verdict": "probed|ignored|not_applicable",
  "reasoning": "one sentence"
}}"""

_MRQ_APPLICABLE = """\
A misconception was detected in the preceding student turn (see MISCONCEPTION \
DETECTED above). Did the tutor's response probe it Socratically?
- "probed": tutor asked a question targeting the misconception KC, guiding the \
  student to discover the error themselves
- "ignored": tutor ignored the misconception, corrected it directly, or moved to \
  an unrelated KC"""

_MRQ_NOT_APPLICABLE = \
    'No misconception in preceding student turn. Return "not_applicable".'


# ---------------------------------------------------------------------------
# Per-turn Haiku classification
# ---------------------------------------------------------------------------

def _classify_tutor_turn(
    tutor_response: str,
    student_message: str,
    preceding_observations: list[dict],
    kg: dict,
    kc_type_map: dict[str, str],
    bkt_snapshot: dict[str, float],
    frontier: list[str],
    compute_nac: bool,
    client: anthropic.Anthropic,
) -> dict:
    """
    Single Haiku call classifying a tutor turn.
    Returns dict with: targeted_kc_id, nac_verdict, observed_type,
    warranted_type, mrq_verdict.
    """
    kcs = kg.get("kcs", [])
    kc_listing = "\n".join(
        f"  {kc['id']}: {kc['name']} [{kc_type_map.get(kc['id'], 'concept')}]"
        for kc in kcs
    )
    frontier_listing = (
        "\n".join(f"  {kc_id}" for kc_id in frontier)
        or "  (none)"
    )
    # BKT state: frontier KCs + any with p >= 0.4
    relevant = set(frontier) | {kc_id for kc_id, p in bkt_snapshot.items() if p >= 0.4}
    bkt_state = "\n".join(
        f"  {kc_id}: {round(p, 3)}"
        for kc_id, p in sorted(bkt_snapshot.items())
        if kc_id in relevant
    ) or "  (no BKT data)"

    misconceptions = [
        obs for obs in preceding_observations
        if obs.get("observation_class") == "misconception"
    ]
    if misconceptions:
        misc_lines = "\n".join(
            f"  KC: {m['kc_id']}, evidence: {m.get('evidence_quote', '')[:120]}"
            for m in misconceptions
        )
        misconception_section = f"MISCONCEPTION DETECTED:\n{misc_lines}\n\n"
        mrq_instructions = _MRQ_APPLICABLE
    else:
        misconception_section = ""
        mrq_instructions = _MRQ_NOT_APPLICABLE

    prompt = _TUTOR_CLASSIFIER_PROMPT.format(
        kc_listing=kc_listing,
        frontier_listing=frontier_listing,
        bkt_state=bkt_state,
        misconception_section=misconception_section,
        student_message=(student_message[:500] if student_message
                         else "(no preceding student message)"),
        tutor_response=tutor_response[:1000],
        mrq_instructions=mrq_instructions,
    )

    try:
        response = _create_with_retry(
            client,
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
    except Exception as e:
        print(f"  [tutor-classifier] turn error: {e}", file=sys.stderr)
        data = {}

    valid_types = {"concept", "convention", "narrative"}
    nac = data.get("nac_verdict")
    mrq = data.get("mrq_verdict")
    return {
        "targeted_kc_id": data.get("targeted_kc_id") or None,
        "nac_verdict": (
            nac if (compute_nac and nac in ("compliant", "violation")) else "disabled"
        ),
        "observed_type": data.get("observed_type") if data.get("observed_type") in valid_types else None,
        "warranted_type": data.get("warranted_type") if data.get("warranted_type") in valid_types else None,
        "mrq_verdict": mrq if mrq in ("probed", "ignored") else None,
    }


# ---------------------------------------------------------------------------
# KC status (pure Python)
# ---------------------------------------------------------------------------

def _compute_kc_status(
    targeted_kc_id: str | None,
    bkt_snapshot: dict[str, float],
    frontier: list[str],
    kg: dict,
) -> str:
    all_kc_ids = {kc["id"] for kc in kg.get("kcs", [])}
    if not targeted_kc_id or targeted_kc_id not in all_kc_ids:
        return "off_map"
    p = bkt_snapshot.get(targeted_kc_id, 0.0)
    if p >= 0.7:
        return "mastered"
    if targeted_kc_id in frontier:
        return "on_frontier"
    return "prereqs_not_met"


# ---------------------------------------------------------------------------
# PR stall detection (pure Python)
# ---------------------------------------------------------------------------

def _detect_stalls(
    turn_results: list[TurnResult],
    K: int = 3,
    delta: float = 0.05,
) -> None:
    """
    Mark stall turns in-place (Option B: from the K-th qualifying step onward).

    Shape 1 — mastered KC drilled: p_mastered >= 0.7 on this turn.
    Shape 2 — frontier stall: same KC, p < 0.7, Δp < delta from previous turn.

    The first turn of a KC run sets the baseline for Shape 2 delta but is not
    itself a qualifying step. Shape 1 begins counting from the first turn.
    Misconception in preceding_observations resets the stall counter.
    """
    current_kc: str | None = None
    stall_run_length: int = 0
    prev_p: float | None = None

    for tr in turn_results:
        kc = tr.targeted_kc_id
        p = tr.bkt_snapshot.get(kc) if kc else None
        has_misconception = any(
            obs.get("observation_class") == "misconception"
            for obs in tr.preceding_observations
        )

        if kc != current_kc or has_misconception:
            # New KC or misconception reset
            current_kc = kc
            prev_p = p
            # Misconception exemption always resets to 0 — the turn is productive
            # regardless of p_mastered.  For a plain new-KC first turn, count it
            # if p >= 0.7 (Shape 1 can be detected from turn 1 without a delta).
            stall_run_length = 0 if has_misconception else (
                1 if (kc and p is not None and p >= 0.7) else 0
            )
            continue

        # Same KC, no misconception — check stall condition for this step
        stall_shape: str | None = None
        if p is not None:
            if p >= 0.7:
                stall_shape = "shape1"
            elif prev_p is not None and (p - prev_p) < delta:
                stall_shape = "shape2"

        if stall_shape is None:
            stall_run_length = 0
        else:
            stall_run_length += 1
            if stall_run_length >= K:
                tr.is_stall_turn = True
                tr.stall_shape = stall_shape

        prev_p = p


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_transcript(
    analysis_input: dict,
    client: anthropic.Anthropic | None = None,
    compute_nac: bool = True,
) -> EvaluationResult:
    """
    Run post-hoc evaluation on a single session transcript.

    analysis_input must match the shape returned by
    GET /api/admin/sessions/{session_id}/analysis-input.

    compute_nac=False: all nac_verdict fields set to "disabled", nac=1.0.
    """
    # Avoid circular import — metrics imports TurnResult from this module
    from tutor_eval.evaluation.metrics import (
        compute_composite,
        compute_kft,
        compute_lcq,
        compute_mrq,
        compute_nac as _compute_nac,
        compute_pr,
    )

    if client is None:
        client = anthropic.Anthropic()

    session_id = analysis_input["session_id"]
    article_id = analysis_input["article_id"]
    domain_map = analysis_input["domain_map"]
    bkt_initial_states_raw = analysis_input.get("bkt_initial_states", {})
    lesson_turns = analysis_input.get("lesson_turns", [])

    kg = _build_kg(domain_map)
    target_kcs = _get_target_kcs_from_dm(domain_map)
    kc_type_map = _build_kc_type_map(domain_map)
    bkt_states = _init_bkt_from_raw(bkt_initial_states_raw, kg)

    turn_results: list[TurnResult] = []
    pending_observations: list[dict] = []
    last_student_message: str = ""

    for raw_turn in lesson_turns:
        role = raw_turn.get("role")
        turn_number = raw_turn.get("turn_number", 0)
        content = raw_turn.get("content", "")

        if role == "user":
            evaluator_snap = raw_turn.get("evaluator_snapshot")
            if evaluator_snap:
                # Simulation shortcut: use pre-computed BKT output
                observations = evaluator_snap.get("observations", [])
                for kc_id, p in evaluator_snap.get("updated_bkt", {}).items():
                    if kc_id in bkt_states:
                        bkt_states[kc_id].p_mastered = p
                    else:
                        bkt_states[kc_id] = BKTState(
                            kc_id=kc_id, p_mastered=p, knowledge_class="absent"
                        )
            else:
                frontier = get_knowledge_frontier(bkt_states, kg, target_kcs)
                observations = classify_observations(
                    content,
                    kcs=kg.get("kcs", []),
                    client=client,
                    target_kcs=target_kcs,
                    frontier=frontier,
                    kg=kg,
                )
                observed_kcs = {obs["kc_id"] for obs in observations}
                for obs in observations:
                    kc_id = obs["kc_id"]
                    if kc_id in bkt_states:
                        update_bkt(bkt_states[kc_id], obs["observation_class"])
                    else:
                        state = BKTState(
                            kc_id=kc_id, p_mastered=P_L0["absent"], knowledge_class="absent"
                        )
                        update_bkt(state, obs["observation_class"])
                        bkt_states[kc_id] = state
                for kc_id, state in bkt_states.items():
                    if kc_id not in observed_kcs:
                        update_bkt(state, "absent")

            pending_observations = observations
            last_student_message = content

        elif role == "tutor":
            bkt_snap = {
                kc_id: round(state.p_mastered, 4)
                for kc_id, state in bkt_states.items()
            }
            frontier = get_knowledge_frontier(bkt_states, kg, target_kcs)

            classification = _classify_tutor_turn(
                tutor_response=content,
                student_message=last_student_message,
                preceding_observations=pending_observations,
                kg=kg,
                kc_type_map=kc_type_map,
                bkt_snapshot=bkt_snap,
                frontier=frontier,
                compute_nac=compute_nac,
                client=client,
            )

            targeted_kc_id = classification["targeted_kc_id"]
            kc_status = _compute_kc_status(targeted_kc_id, bkt_snap, frontier, kg)

            tr = TurnResult(
                turn_number=turn_number,
                targeted_kc_id=targeted_kc_id,
                kc_status=kc_status,
                nac_verdict=classification["nac_verdict"],
                reviewer_verdict=raw_turn.get("reviewer_verdict"),
                observed_type=classification["observed_type"],
                warranted_type=classification["warranted_type"],
                mrq_verdict=classification["mrq_verdict"],
                bkt_snapshot=bkt_snap,
                preceding_observations=list(pending_observations),
            )
            turn_results.append(tr)
            pending_observations = []

    _detect_stalls(turn_results)

    reviewer_active = any(
        tr.reviewer_verdict in ("pass", "warn", "fail") for tr in turn_results
    )
    reviewer_rewrite_count = sum(
        1 for tr in turn_results if tr.reviewer_verdict == "fail"
    )

    total_tutor_turns = len(turn_results)
    is_valid = total_tutor_turns >= 8
    invalidity_reason = (
        f"only {total_tutor_turns} tutor turns (minimum 8)" if not is_valid else None
    )

    nac = _compute_nac(turn_results)
    kft = compute_kft(turn_results)
    pr = compute_pr(turn_results)
    lcq = compute_lcq(turn_results)
    mrq = compute_mrq(turn_results)
    mrq_adjustment = 0.15 * (mrq - 0.5) if mrq is not None else 0.0
    composite = compute_composite(nac, kft, pr, lcq, mrq_adjustment)

    return EvaluationResult(
        session_id=session_id,
        article_id=article_id,
        turn_results=turn_results,
        nac=nac,
        kft=kft,
        pr=pr,
        lcq=lcq,
        mrq=mrq,
        mrq_adjustment=mrq_adjustment,
        composite=composite,
        total_tutor_turns=total_tutor_turns,
        is_valid=is_valid,
        invalidity_reason=invalidity_reason,
        reviewer_active=reviewer_active,
        reviewer_rewrite_count=reviewer_rewrite_count,
    )
