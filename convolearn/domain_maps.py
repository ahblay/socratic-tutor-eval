"""
convolearn/domain_maps.py

Stage 2: Generate (and cache) domain maps for each unique question prompt.
Delegates entirely to resolve_domain_map(), which handles caching internally.
"""

from __future__ import annotations

import sys
from pathlib import Path

import anthropic

from tutor_eval.ingestion.domain_resolver import DEFAULT_CACHE_DIR, resolve_domain_map


def generate_domain_maps(
    sampled_prompts: list[dict],
    client: anthropic.Anthropic,
    cache_dir: Path | None = None,
) -> dict[str, dict]:
    """
    Generate domain maps for each unique prompt_id.

    Returns {prompt_id: domain_map}.
    Caching is handled by resolve_domain_map — re-running costs nothing.
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
        dm = resolve_domain_map(raw, client, cache_dir=cache_dir)
        domain_maps[prompt_id] = dm

    return domain_maps
