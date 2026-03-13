"""
webapp/services/assessment_service.py

Pure functions for the pre-session assessment flow:
  - Follow-up KC selection (graph root detection)
  - Student response classification via Haiku
  - L0 propagation through the prerequisite graph
"""

from __future__ import annotations

import json
import re

import anthropic

from webapp.services.domain_cache import build_kg_from_domain_map, _slugify

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPENER_KC_ID = "__opener__"
OPENER_TEXT = "Before we begin, briefly describe what you already know about {topic}."
MAX_FOLLOWUPS = 3
MAX_QUESTIONS = 4  # opener + 3 follow-ups

L0_VALUES: dict[str, float] = {
    "mastered": 0.90,
    "partial":  0.25,
    "absent":   0.10,
}

# ---------------------------------------------------------------------------
# Classification prompts
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """\
You are an educational assessment assistant. A student was asked about a specific \
knowledge concept and gave the free-text response below. Classify their level of \
understanding of that concept.

CONCEPT: {kc_name}
CONCEPT DESCRIPTION: {kc_description}

STUDENT RESPONSE:
{student_response}

Choose exactly one classification:
- mastered : Student explains the concept correctly and clearly in their own words,
             demonstrating genuine understanding without prompting.
- partial  : Student shows some awareness — mentions relevant terms or a partial idea —
             but the explanation is incomplete, vague, or missing key details.
- absent   : Student shows no meaningful understanding: says "I don't know", gives a
             completely off-topic response, or does not engage with the concept at all.

Respond with a JSON object and nothing else:
{{"classification": "<mastered|partial|absent>", "confidence": <0.0-1.0>, \
"evidence": "<one sentence quoting or paraphrasing the key signal from their response>"}}"""

_OPENER_CLASSIFY_PROMPT = """\
A student was asked to describe what they already know about "{topic}". \
Their response is below. Classify their overall prior knowledge level.

STUDENT RESPONSE:
{student_response}

Choose exactly one:
- mastered : Student gives a detailed, accurate overview demonstrating strong prior knowledge.
- partial  : Student shows some relevant knowledge but is incomplete or mixed with gaps.
- absent   : Student has little or no relevant prior knowledge.

Respond with JSON only:
{{"classification": "<mastered|partial|absent>", \
"evidence": "<brief quote or paraphrase showing the key signal>"}}"""

# ---------------------------------------------------------------------------
# Follow-up KC selection
# ---------------------------------------------------------------------------

def select_followup_kcs(domain_map: dict, max_followups: int = MAX_FOLLOWUPS) -> list[dict]:
    """
    Return up to max_followups KC dicts targeting foundational KCs.

    Foundational KCs = those in recommended_sequence that are NOT depended on
    by any other concept (i.e., graph roots — nothing is a prerequisite FOR them).
    Falls back to first N in recommended_sequence if no roots found.

    Returns list of dicts: {"kc_id": str, "kc_name": str, "question_text": str}
    """
    concepts = domain_map.get("core_concepts", [])
    sequence = domain_map.get("recommended_sequence", [])
    checkpoint_questions = domain_map.get("checkpoint_questions", [])

    # Build a lookup: concept_name → checkpoint question text
    checkpoint_by_concept: dict[str, str] = {}
    for cq in checkpoint_questions:
        after = cq.get("after_concept", "")
        question = cq.get("question", "")
        if after and question:
            checkpoint_by_concept[after] = question

    # Build the set of concept names that are downstream of something
    # (i.e., they appear as a target in some concept's prerequisite_for list)
    has_upstream: set[str] = set()
    for c in concepts:
        for downstream in c.get("prerequisite_for", []):
            has_upstream.add(downstream)

    # Graph roots = in sequence but NOT downstream of anything
    foundational: list[str] = [
        name for name in sequence if name not in has_upstream
    ]

    # Fall back to first N if no roots found (e.g. linear chain with no true root)
    candidates = foundational if foundational else sequence

    selected = candidates[:max_followups]

    result = []
    for name in selected:
        kc_id = _slugify(name)
        question_text = checkpoint_by_concept.get(
            name,
            f"Can you explain what {name} means in your own words?"
        )
        result.append({"kc_id": kc_id, "kc_name": name, "question_text": question_text})

    return result


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

async def classify_opener_answer(
    student_response: str,
    topic: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    """Classify opener response into mastered/partial/absent. Returns 'absent' on failure."""
    prompt = _OPENER_CLASSIFY_PROMPT.format(
        topic=topic,
        student_response=student_response[:1500],
    )
    return await _run_classify(prompt, client, model)


async def classify_assessment_answer(
    student_response: str,
    kc_name: str,
    kc_description: str,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    """Classify a KC-specific answer into mastered/partial/absent. Returns 'absent' on failure."""
    prompt = _CLASSIFY_PROMPT.format(
        kc_name=kc_name,
        kc_description=kc_description[:500],
        student_response=student_response[:1500],
    )
    return await _run_classify(prompt, client, model)


async def _run_classify(
    prompt: str,
    client: anthropic.AsyncAnthropic,
    model: str,
) -> str:
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        classification = data.get("classification", "absent")
        if classification not in ("mastered", "partial", "absent"):
            return "absent"
        return classification
    except Exception:
        return "absent"


# ---------------------------------------------------------------------------
# L0 propagation
# ---------------------------------------------------------------------------

def propagate_l0(
    domain_map: dict,
    assessed_kcs: dict[str, str],   # {kc_id: "mastered"|"partial"|"absent"}
    global_prior: str,              # opener classification
) -> dict[str, float]:
    """
    Compute L0 estimates for all KCs in the domain map.

    Phase 1: Seed directly assessed KCs from their observation class.
    Phase 2: Fill unassessed KCs with the global prior (opener classification).
    Phase 3: Upward propagation — if student knows a concept, raise its prerequisites.
    Phase 4: Downward propagation — if student doesn't know a prereq, lower its dependents.
    Phase 5: Clamp all values to [0.01, 0.99].
    """
    kg = build_kg_from_domain_map(domain_map)
    all_kc_ids = [kc["id"] for kc in kg["kcs"]]
    edges = kg["edges"]  # [{from: prereq_slug, to: dependent_slug}]

    prior_value = L0_VALUES.get(global_prior, L0_VALUES["absent"])

    # Phases 1 & 2: seed
    l0: dict[str, float] = {}
    for kc_id in all_kc_ids:
        if kc_id in assessed_kcs:
            l0[kc_id] = L0_VALUES.get(assessed_kcs[kc_id], L0_VALUES["absent"])
        else:
            l0[kc_id] = prior_value

    # Phase 3: mastered dependent → raise its prerequisites
    for edge in edges:
        prereq, dep = edge["from"], edge["to"]
        if assessed_kcs.get(dep) == "mastered" and prereq in l0:
            l0[prereq] = max(l0[prereq], L0_VALUES["mastered"])

    # Phase 4: absent prerequisite → lower its dependents
    for edge in edges:
        prereq, dep = edge["from"], edge["to"]
        if assessed_kcs.get(prereq) == "absent" and dep in l0 and dep not in assessed_kcs:
            l0[dep] = min(l0[dep], L0_VALUES["absent"])

    # Phase 5: clamp
    return {k: max(0.01, min(0.99, v)) for k, v in l0.items()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def class_from_l0(p: float) -> str:
    if p >= 0.70:
        return "mastered"
    elif p >= 0.30:
        return "partial"
    return "absent"


def kc_description_for(kc_name: str, domain_map: dict) -> str:
    """Look up a KC's description from the domain map by name."""
    for c in domain_map.get("core_concepts", []):
        if c.get("concept") == kc_name:
            return c.get("description", "")
    return ""
