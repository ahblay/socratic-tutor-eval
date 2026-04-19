"""
tutor_eval/ingestion/converter.py

Converts a validated raw transcript dict + resolved domain map into the
analysis_input format consumed by analyze_transcript().
"""

from __future__ import annotations

import re
import uuid


# ---------------------------------------------------------------------------
# BKT preset helpers
# ---------------------------------------------------------------------------

def _derive_slug(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")[:64]


def _make_bkt_states(domain_map: dict, preset: str) -> dict:
    """
    Build bkt_initial_states from the domain map using a preset strategy.

    preset="absent"          — all KCs start at p=0.10 (default, no prior knowledge)
    preset="prereqs_mastered" — root KCs (no incoming edges) at p=0.90, others p=0.10
    preset="all_partial"     — all KCs at p=0.50
    """
    # Build minimal KG to identify roots
    concepts = domain_map.get("core_concepts", [])
    name_to_slug = {
        c["concept"]: _derive_slug(c["concept"])
        for c in concepts if c.get("concept")
    }
    all_kc_ids = set(name_to_slug.values())

    # KCs that are pointed to by some other KC's prerequisite_for
    has_incoming: set[str] = set()
    for c in concepts:
        for downstream in c.get("prerequisite_for", []):
            to_slug = name_to_slug.get(downstream)
            if to_slug:
                has_incoming.add(to_slug)

    states: dict[str, dict] = {}
    for kc_id in all_kc_ids:
        if preset == "prereqs_mastered":
            if kc_id not in has_incoming:
                # Root node — assumed prerequisite knowledge
                p, klass = 0.90, "mastered"
            else:
                p, klass = 0.10, "absent"
        elif preset == "all_partial":
            p, klass = 0.50, "partial"
        else:  # "absent" (default)
            p, klass = 0.10, "absent"

        states[kc_id] = {
            "p_mastered": p,
            "knowledge_class": klass,
            "observation_history": [],
        }
    return states


# ---------------------------------------------------------------------------
# Main converter
# ---------------------------------------------------------------------------

def prepare_analysis_input(raw: dict, domain_map: dict) -> dict:
    """
    Convert a raw transcript dict and resolved domain map into the
    analysis_input format expected by analyze_transcript().

    Role normalization: "student" → "user" (analyzer uses "user"/"tutor").
    turn_number is assigned sequentially starting at 1.
    reviewer_verdict, tutor_state_snapshot, evaluator_snapshot are all
    set to None (not available for external transcripts).
    """
    session_id = raw.get("session_id") or str(uuid.uuid4())
    # article_id used as an identifier in EvaluationResult; derive from topic
    article_id = raw.get("source") or _derive_slug(raw.get("topic", "unknown"))

    # --- Normalize turns ---
    lesson_turns = []
    for i, t in enumerate(raw.get("turns", [])):
        role = t.get("role", "")
        if role == "student":
            role = "user"
        elif role == "teacher":
            role = "tutor"
        content = t.get("content", "")
        lesson_turns.append({
            "turn_number": i + 1,
            "role": role,
            "content": content,
            "raw_content": t.get("raw_content", content),
            "reviewer_verdict": t.get("reviewer_verdict"),
            "tutor_state_snapshot": None,
            "evaluator_snapshot": None,
        })

    # --- BKT initial states ---
    bkt_initial_states = raw.get("bkt_initial_states") or {}
    if not bkt_initial_states:
        preset = raw.get("bkt_preset") or "absent"
        if preset in ("absent", "prereqs_mastered", "all_partial"):
            bkt_initial_states = _make_bkt_states(domain_map, preset)
        else:
            bkt_initial_states = _make_bkt_states(domain_map, "absent")

    return {
        "session_id": session_id,
        "article_id": article_id,
        "article_title": raw.get("topic", ""),
        "domain_map": domain_map,
        "bkt_initial_states": bkt_initial_states,
        "assessment_turns": [],
        "lesson_turns": lesson_turns,
    }
