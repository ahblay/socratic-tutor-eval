"""
convolearn/domain_maps.py

Stage 2: Generate (and cache) domain maps for each unique question prompt.
Delegates to resolve_domain_map(), which handles caching internally.

When slim=True (the default for --domain-source sentence):
  - Skips the enrichment pass (no concept splitting, no reference_material)
  - Uses a reduced KC target (4–7 concepts) calibrated to 10-15 exchange sessions
  - Trims the result to essential KCs only and strips non-evaluation fields
  - Uses a separate cache key suffix (-slim) to avoid colliding with full webapp maps
"""

from __future__ import annotations

import sys
from pathlib import Path

import anthropic

from tutor_eval.ingestion.domain_resolver import DEFAULT_CACHE_DIR, resolve_domain_map

_SLIM_CONCEPTS_HINT = (
    "Aim for 4–7 concepts. This domain map will evaluate a brief introductory "
    "Q&A session of 10–15 exchanges. Include ONLY the concepts that are the "
    "core teaching content — what a teacher would spend most of the session "
    "discussing and explaining. Do NOT include foundational prerequisites that "
    "the teacher might mention briefly but not teach in depth. Prefer concepts "
    "directly reachable from the question itself in 2–3 exchanges."
)

_EVAL_FIELDS = {"concept", "prerequisite_for", "knowledge_type"}


def _slim_domain_map(dm: dict, max_kcs: int = 7) -> dict:
    """
    Trim a generated domain map to evaluation-ready size for ConvoLearn.

    1. Filter core_concepts to depth_priority "essential"; add "important" if fewer than max_kcs.
    2. Follow recommended_sequence order, cap at max_kcs.
    3. Strip fields not used by the analyzer (reference_material, description, etc.).
    4. Prune prerequisite_for references to KCs no longer in the slim set.
    """
    concepts = dm.get("core_concepts", [])
    sequence = dm.get("recommended_sequence", [])

    by_priority: dict[str, list[str]] = {}
    for c in concepts:
        p = c.get("depth_priority", "important")
        by_priority.setdefault(p, []).append(c.get("concept", ""))

    keep: set[str] = set(by_priority.get("essential", []))
    if len(keep) < max_kcs:
        keep |= set(by_priority.get("important", []))

    slim_seq = [name for name in sequence if name in keep][:max_kcs]
    slim_names = set(slim_seq)

    slim_concepts = [
        {
            "concept": c["concept"],
            "knowledge_type": c.get("knowledge_type", "concept"),
            "prerequisite_for": [
                ref for ref in c.get("prerequisite_for", []) if ref in slim_names
            ],
        }
        for c in concepts
        if c.get("concept") in slim_names
    ]

    return {
        "topic": dm.get("topic", ""),
        "core_concepts": slim_concepts,
        "recommended_sequence": slim_seq,
        "common_misconceptions": dm.get("common_misconceptions", []),
    }


def generate_domain_maps(
    sampled_prompts: list[dict],
    client: anthropic.Anthropic,
    cache_dir: Path | None = None,
    slim: bool = False,
) -> dict[str, dict]:
    """
    Generate domain maps for each unique prompt_id.

    Returns {prompt_id: domain_map}.
    Caching is handled by resolve_domain_map — re-running costs nothing.

    slim=True: use the compact 4–7 KC mode suited for ConvoLearn's brief Q&A sessions.
    slim=False: generate a full 12–20 KC enriched map (webapp default).
    """
    if cache_dir is None:
        cache_dir = DEFAULT_CACHE_DIR

    domain_maps: dict[str, dict] = {}
    total = len(sampled_prompts)
    for i, entry in enumerate(sampled_prompts, 1):
        prompt_id = entry["prompt_id"]
        question_prompt = entry["question_prompt"]
        print(
            f"[domain-maps] ({i}/{total}) {prompt_id!r}",
            file=sys.stderr,
            flush=True,
        )
        raw = {"topic": question_prompt}
        dm = resolve_domain_map(
            raw,
            client,
            cache_dir=cache_dir,
            skip_enrich=slim,
            target_concepts_hint=_SLIM_CONCEPTS_HINT if slim else None,
        )
        if slim:
            dm = _slim_domain_map(dm)
        domain_maps[prompt_id] = dm

    return domain_maps
