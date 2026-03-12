"""
webapp/services/domain_cache.py

DB-backed domain map cache.  Replaces the file-based .socratic-domain-cache/
for the web server.  Keyed on wikipedia_page_id (stable across title renames).

build_kg_from_domain_map() translates the domain mapper's JSON output format
into the {"kcs": [...], "edges": [...]} format that BKTEvaluator expects.
"""

from __future__ import annotations

import re

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

    # compute_domain_map is sync — run it in a thread
    import asyncio
    sync_client = anthropic.Anthropic(api_key=client.api_key)
    domain_map = await asyncio.to_thread(compute_domain_map, article_text, sync_client)

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
