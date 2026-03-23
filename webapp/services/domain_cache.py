"""
webapp/services/domain_cache.py

DB-backed domain map cache.  Replaces the file-based .socratic-domain-cache/
for the web server.  Keyed on wikipedia_page_id (stable across title renames).

build_kg_from_domain_map() translates the domain mapper's JSON output format
into the {"kcs": [...], "edges": [...]} format that BKTEvaluator expects.
"""

from __future__ import annotations

import json
import re
import sys

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tutor_eval.tutors.socratic import compute_domain_map
from webapp.db.models import Article


async def get_or_create_domain_map(
    article: Article,
    article_text: str,
    client: anthropic.AsyncAnthropic,
    db: AsyncSession,
) -> dict:
    """
    Return the domain map for an article, computing it if not yet cached.

    Updates article.domain_map and article.domain_map_status in the DB.
    Caller is responsible for committing the session.
    """
    if article.domain_map and article.domain_map_status == "ready":
        return article.domain_map

    article.domain_map_status = "pending"
    await db.flush()

    # compute_domain_map and the follow-up fix pass are both sync — run together
    import asyncio
    sync_client = anthropic.Anthropic(api_key=client.api_key)

    def _compute_and_fix(text: str) -> dict:
        dm = compute_domain_map(text, sync_client)          # Pass 1: concept list + knowledge_type
        return _fix_prerequisite_references(dm, sync_client)  # Pass 2: canonicalise edges

    domain_map = await asyncio.to_thread(_compute_and_fix, article_text)

    article.domain_map = domain_map
    article.domain_map_status = "ready"
    article.domain_map_version = (article.domain_map_version or 0) + 1
    await db.flush()

    return domain_map


async def invalidate_domain_map(
    wikipedia_page_id: int,
    db: AsyncSession,
) -> bool:
    """Force recomputation on next access.  Returns True if article existed."""
    result = await db.execute(
        select(Article).where(Article.wikipedia_page_id == wikipedia_page_id)
    )
    article = result.scalar_one_or_none()
    if article is None:
        return False
    article.domain_map_status = "pending"
    article.domain_map = None
    await db.flush()
    return True


def _fix_prerequisite_references(
    domain_map: dict,
    client: anthropic.Anthropic,
) -> dict:
    """
    Second-pass LLM call: rewrite every prerequisite_for entry so it exactly
    matches a canonical concept name from core_concepts, or drop it if no
    match exists.

    Only fires when at least one reference is already inconsistent; otherwise
    returns the original dict unchanged.  Runs once at domain-map creation
    time and is then cached permanently — there is no per-session cost.

    Uses Haiku (fast, cheap) because the task is purely mechanical.
    """
    concepts = domain_map.get("core_concepts", [])
    canonical_names: list[str] = [c["concept"] for c in concepts if c.get("concept")]

    if not canonical_names:
        return domain_map

    # Fast path: if every reference is already an exact canonical name, skip.
    name_set = set(canonical_names)
    all_resolved = all(
        ref in name_set
        for c in concepts
        for ref in c.get("prerequisite_for", [])
    )
    if all_resolved:
        return domain_map

    canonical_list = "\n".join(f"- {n}" for n in canonical_names)
    prompt = f"""\
You are correcting a domain map JSON object.

The domain map defines these concepts (canonical names — the only valid values):
{canonical_list}

Some entries inside "prerequisite_for" arrays do not exactly match the canonical
names above.  Your task: rewrite every "prerequisite_for" array so that each
entry is either:
  1. An exact string match of one of the canonical names listed above, OR
  2. Removed entirely if it does not clearly correspond to any canonical concept.

Do not change anything else in the JSON (descriptions, ordering, other fields).
Return only the corrected JSON object with no explanation, no markdown fences.

Domain map to correct:
{json.dumps(domain_map)}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()

        # Strip markdown code fences if the model adds them despite instructions
        if text.startswith("```"):
            lines = text.splitlines()
            # Drop opening fence (```json or ```) and closing fence (```)
            start = 1
            end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
            text = "\n".join(lines[start:end]).strip()

        fixed = json.loads(text)

        # Sanity check: the fixed map must still contain the same concepts.
        fixed_names = {c.get("concept") for c in fixed.get("core_concepts", [])}
        if fixed_names != name_set:
            print(
                "  [domain-map-fix] concept set changed after fix pass — "
                "discarding fix and using original",
                file=sys.stderr,
            )
            return domain_map

        return fixed

    except Exception as exc:
        # Fail safe: if anything goes wrong, return the original unmodified.
        print(f"  [domain-map-fix] fix pass failed ({exc}) — using original", file=sys.stderr)
        return domain_map


def build_kg_from_domain_map(domain_map: dict) -> dict:
    """
    Translate domain mapper output → BKTEvaluator KC graph format.

    Domain mapper produces:
        core_concepts: [{concept, description, prerequisite_for, ...}, ...]
        recommended_sequence: [concept_name, ...]

    BKTEvaluator expects:
        {"kcs": [{"id": slug, "name": name}, ...],
         "edges": [{"from": slug, "to": slug}, ...]}

    prerequisite_for is an array of concept names that depend on this concept,
    so concept → prerequisite_for[i] becomes edge {from: concept, to: prereq_for[i]}.
    """
    concepts = domain_map.get("core_concepts", [])

    # Build name → slug mapping
    name_to_slug: dict[str, str] = {}
    for c in concepts:
        name = c.get("concept", "")
        name_to_slug[name] = _slugify(name)

    kcs = [
        {"id": name_to_slug[c["concept"]], "name": c["concept"]}
        for c in concepts
        if c.get("concept")
    ]

    edges: list[dict] = []
    for c in concepts:
        from_slug = name_to_slug.get(c.get("concept", ""))
        if not from_slug:
            continue
        for downstream_name in c.get("prerequisite_for", []):
            to_slug = name_to_slug.get(downstream_name)
            if to_slug and to_slug != from_slug:
                edges.append({"from": from_slug, "to": to_slug})

    return {"kcs": kcs, "edges": edges}


def get_target_kcs(domain_map: dict) -> list[str]:
    """Return KC IDs in recommended_sequence order."""
    sequence = domain_map.get("recommended_sequence", [])
    return [_slugify(name) for name in sequence]


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:64]
