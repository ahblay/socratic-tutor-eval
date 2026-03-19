"""
webapp/services/assessment_service.py

Pure functions for the pre-session assessment flow:
  - Follow-up question generation via LLM (contextual, conversational)
  - Opener classification (global prior for L0 propagation)
  - Holistic end-of-assessment classification across all KCs
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

OPENER_KC_ID  = "__opener__"
OPENER_TEXT   = "Before we begin, briefly describe what you already know about {topic}."
MAX_FOLLOWUPS = 5
MAX_QUESTIONS = 6  # opener + 5 follow-ups

L0_VALUES: dict[str, float] = {
    "mastered": 0.90,
    "partial":  0.25,
    "absent":   0.10,
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

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

_FOLLOWUP_PROMPT = """\
You are assessing a student's prior knowledge before a tutoring session on "{topic}".

Conversation so far:
{conversation}

The lesson covers these knowledge components:
{kc_summary}

Ask one natural follow-up question to better understand the student's prior knowledge. \
The question should:
- Explore an aspect not yet covered in the conversation
- Sound conversational, not like a formal test item
- Not include hints, corrections, or explanations — only probe

Reply with just the question text, nothing else."""

_HOLISTIC_CLASSIFY_PROMPT = """\
You conducted a brief prior-knowledge assessment before a tutoring session on "{topic}".

Full assessment conversation:
{conversation}

The lesson covers these knowledge components:
{kc_list}

For each knowledge component, classify the student's demonstrated prior knowledge:
- "mastered" : student clearly and correctly described this concept in their own words
- "partial"  : student showed some relevant awareness but incomplete or vague
- "absent"   : no meaningful knowledge of this concept was demonstrated

Default to "absent" for any concept the student did not address.

Return a JSON object mapping each kc_id slug to its classification, for every KC listed:
{{"<kc_id_slug>": "mastered|partial|absent", ...}}"""

# ---------------------------------------------------------------------------
# Question generation
# ---------------------------------------------------------------------------

async def generate_followup_question(
    conversation: list[dict],
    topic: str,
    domain_map: dict,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    """Generate a natural contextual follow-up question from the conversation so far."""
    concepts = domain_map.get("core_concepts", [])
    kc_summary = "\n".join(
        f"- {c['concept']}: {c.get('description', '')[:120]}"
        for c in concepts[:15]
    )
    conv_text = "\n".join(
        f"{'Tutor' if t['role'] == 'tutor' else 'Student'}: {t['text']}"
        for t in conversation
    )
    prompt = _FOLLOWUP_PROMPT.format(
        topic=topic,
        conversation=conv_text,
        kc_summary=kc_summary,
    )
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception:
        return "Can you share anything else about your background with this topic?"


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


async def classify_full_assessment(
    conversation: list[dict],
    topic: str,
    domain_map: dict,
    client: anthropic.AsyncAnthropic,
    model: str = "claude-haiku-4-5-20251001",
) -> dict[str, str]:
    """
    Holistic end-of-assessment classification.
    Returns {kc_slug: 'mastered'|'partial'|'absent'} for all KCs in the domain map.
    """
    concepts = domain_map.get("core_concepts", [])
    kc_list = "\n".join(
        f"- {_slugify(c['concept'])} ({c['concept']}): {c.get('description', '')[:150]}"
        for c in concepts
    )
    conv_text = "\n".join(
        f"{'Tutor' if t['role'] == 'tutor' else 'Student'}: {t['text']}"
        for t in conversation
    )
    prompt = _HOLISTIC_CLASSIFY_PROMPT.format(
        topic=topic,
        conversation=conv_text,
        kc_list=kc_list,
    )
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        valid = {"mastered", "partial", "absent"}
        known_ids = {_slugify(c["concept"]) for c in concepts}
        return {k: v for k, v in data.items() if k in known_ids and v in valid}
    except Exception:
        return {}


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
