"""
tutor_eval/ingestion/domain_resolver.py

Domain map resolution for raw transcripts.

Priority order for domain map source:
  1. Inline domain_map in the raw transcript (user-provided; normalized, not regenerated)
  2. wikipedia_url — fetches article text, generates + enriches domain map
  3. topic string (default) — generates + enriches domain map from the phrase alone

All generated maps are cached under cache_dir (default: ~/.socratic-domain-cache/).
Re-running on the same topic/URL costs nothing after the first call.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import anthropic

if TYPE_CHECKING:
    pass

DEFAULT_CACHE_DIR = Path.home() / ".socratic-domain-cache"

# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_VALID_KNOWLEDGE_TYPES = {"concept", "convention", "narrative"}


def _normalize_concept(c: dict | str) -> dict | None:
    """
    Normalize a single concept entry to the webapp format:
      {concept, prerequisite_for, knowledge_type, ...extra}

    Returns None if the entry cannot be interpreted.
    """
    if isinstance(c, str):
        return {
            "concept": c,
            "prerequisite_for": [],
            "knowledge_type": "concept",
        }
    if not isinstance(c, dict):
        return None

    # Accept any of these field names for the concept label
    name = (
        c.get("concept")
        or c.get("name")
        or c.get("title")
        or c.get("kc")
        or ""
    )
    if not name:
        return None

    kt = c.get("knowledge_type", "concept")
    if kt not in _VALID_KNOWLEDGE_TYPES:
        kt = "concept"

    normalized = {
        "concept": name,
        "prerequisite_for": c.get("prerequisite_for", []),
        "knowledge_type": kt,
    }
    # Preserve all other original fields (reference_material, etc.)
    for k, v in c.items():
        if k not in normalized:
            normalized[k] = v
    return normalized


def normalize_domain_map(dm: dict) -> dict:
    """
    Accept a domain map in any reasonable format and return one that
    analyze_transcript() can consume.

    Supported input formats:
    - Webapp format: {core_concepts: [{concept, prerequisite_for, knowledge_type}], ...}
    - Phase-structured format: {phase_topics: {Q1: {core_concepts: [...]}, ...}} —
      concepts are collected from all phases into a flat list (deduped by name)
    - Flexible concept field names: "name" or "title" accepted in place of "concept"
    - KG format: {kcs: [{id, name}], edges: [{from, to}]}
    - Simple string list under any of: concepts, knowledge_components, kcs, topics, items

    Missing recommended_sequence defaults to all concept names in order.
    Missing knowledge_type defaults to "concept".
    Missing prerequisite_for defaults to [].
    """
    # --- Pre-processing: phase_topics → flat core_concepts ---
    # Handles domain maps organized by assignment question or lesson phase where
    # core_concepts are nested under phase_topics.{key}.core_concepts.
    if "core_concepts" not in dm and "phase_topics" in dm:
        seen: set[str] = set()
        flat: list[dict] = []
        for phase in dm["phase_topics"].values():
            for c in phase.get("core_concepts", []):
                name = (
                    c.get("concept") or c.get("name") or c.get("title") or c.get("kc") or ""
                )
                if name and name not in seen:
                    flat.append(c)
                    seen.add(name)
        if flat:
            dm = dict(dm)
            dm["core_concepts"] = flat

    # --- Case 1: has core_concepts (webapp format or close variant) ---
    if "core_concepts" in dm:
        raw_concepts = dm["core_concepts"]
        normalized_concepts = []
        for c in raw_concepts:
            nc = _normalize_concept(c)
            if nc is not None:
                normalized_concepts.append(nc)

        seq = dm.get("recommended_sequence") or [c["concept"] for c in normalized_concepts]

        result = dict(dm)
        result["core_concepts"] = normalized_concepts
        result["recommended_sequence"] = seq
        return result

    # --- Case 2: KG format {kcs: [{id, name}], edges: [{from, to}]} ---
    if "kcs" in dm and isinstance(dm["kcs"], list) and dm["kcs"]:
        first = dm["kcs"][0]
        if isinstance(first, dict) and "name" in first:
            kcs = dm["kcs"]
            edges = dm.get("edges", [])
            id_to_name = {kc["id"]: kc["name"] for kc in kcs if "id" in kc and "name" in kc}
            prereq_for: dict[str, list[str]] = {kc["name"]: [] for kc in kcs if "name" in kc}
            for edge in edges:
                fn = id_to_name.get(edge.get("from"), "")
                tn = id_to_name.get(edge.get("to"), "")
                if fn and tn and fn in prereq_for:
                    prereq_for[fn].append(tn)
            core_concepts = [
                {
                    "concept": kc["name"],
                    "prerequisite_for": prereq_for.get(kc["name"], []),
                    "knowledge_type": "concept",
                }
                for kc in kcs if "name" in kc
            ]
            return {
                "topic": dm.get("topic", ""),
                "core_concepts": core_concepts,
                "recommended_sequence": [kc["name"] for kc in kcs if "name" in kc],
                "common_misconceptions": dm.get("common_misconceptions", []),
            }

    # --- Case 3: flat list of strings under a known key ---
    for key in ("concepts", "knowledge_components", "topics", "items"):
        val = dm.get(key)
        if isinstance(val, list) and val and isinstance(val[0], str):
            core_concepts = [
                {"concept": name, "prerequisite_for": [], "knowledge_type": "concept"}
                for name in val
            ]
            return {
                "topic": dm.get("topic", ""),
                "core_concepts": core_concepts,
                "recommended_sequence": list(val),
                "common_misconceptions": [],
            }

    # --- Unknown format — warn and pass through ---
    print(
        "  [domain-normalizer] unrecognized domain map format; passing through as-is. "
        "Evaluation may fail if core_concepts is missing.",
        file=sys.stderr,
    )
    return dm


def _is_enriched(domain_map: dict) -> bool:
    """True if at least one concept has a knowledge_type annotation."""
    for c in domain_map.get("core_concepts", []):
        if c.get("knowledge_type") in _VALID_KNOWLEDGE_TYPES:
            return True
    return False


# ---------------------------------------------------------------------------
# Wikipedia fetching (sync, no webapp dependency)
# ---------------------------------------------------------------------------

def _fetch_wikipedia_text(url: str) -> str:
    """
    Fetch section-structured plain text from a Wikipedia article URL.
    Returns a concatenated text string suitable for domain map generation.
    Raises RuntimeError on failure.
    """
    try:
        import httpx
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise RuntimeError(
            f"Wikipedia fetching requires 'httpx' and 'beautifulsoup4': {e}"
        ) from e

    # Extract title from URL: .../wiki/Some_Article_Title → Some_Article_Title
    match = re.search(r"/wiki/([^#?]+)", url)
    if not match:
        raise RuntimeError(f"Cannot parse Wikipedia title from URL: {url!r}")
    title = match.group(1)

    api_url = (
        f"https://en.wikipedia.org/w/api.php"
        f"?action=parse&page={title}&prop=sections|text&format=json&redirects=1"
    )
    headers = {"User-Agent": "SocraticTutorEval/1.0 (research; contact via GitHub)"}

    # Fetch parse output (sections + HTML)
    resp = httpx.get(api_url, headers=headers, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"Wikipedia API error: {data['error']}")

    parse = data.get("parse", {})
    html = parse.get("text", {}).get("*", "")
    if not html:
        raise RuntimeError(f"Wikipedia returned empty content for {title!r}")

    soup = BeautifulSoup(html, "html.parser")
    # Remove footnote superscripts
    for tag in soup.find_all("sup"):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()
    # Truncate to ~50k chars (domain mapper processes up to ~40k reasonably)
    return text[:50_000]


# ---------------------------------------------------------------------------
# Domain map generation with caching
# ---------------------------------------------------------------------------

def _derive_slug(s: str) -> str:
    slug = s.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:80]


def _cache_key_for_url(url: str) -> str:
    """Derive a filesystem-safe cache key from a Wikipedia URL."""
    match = re.search(r"/wiki/([^#?]+)", url)
    title = match.group(1) if match else url
    return "wiki-" + _derive_slug(title)


def _load_from_cache(cache_file: Path) -> dict | None:
    if not cache_file.exists():
        return None
    try:
        with open(cache_file) as f:
            return json.load(f)
    except Exception:
        return None


def _save_to_cache(cache_file: Path, dm: dict) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(dm, f, indent=2)
    except Exception as e:
        print(f"  [domain-resolver] could not write cache: {e}", file=sys.stderr)


def _generate_and_enrich(
    topic_or_text: str,
    client: anthropic.Anthropic,
    skip_enrich: bool = False,
    target_concepts_hint: str | None = None,
) -> dict:
    """Run pass 1 + pass 2 domain map generation."""
    from tutor_eval.tutors.socratic import (
        compute_domain_map,
        enrich_domain_map,
        _DEFAULT_CONCEPTS_HINT,
    )
    hint = target_concepts_hint if target_concepts_hint is not None else _DEFAULT_CONCEPTS_HINT
    dm = compute_domain_map(topic_or_text, client, target_concepts_hint=hint)
    if not skip_enrich:
        dm = enrich_domain_map(dm, client)
    return dm


def resolve_domain_map(
    raw: dict,
    client: anthropic.Anthropic,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    skip_enrich: bool = False,
    target_concepts_hint: str | None = None,
) -> dict:
    """
    Return a normalized, analysis-ready domain map for the given raw transcript.

    Priority:
      1. raw["domain_map"] — normalize and return (no API calls)
      2. raw["wikipedia_url"] — fetch article text, generate + cache
      3. raw["topic"] (default) — generate from phrase + cache
    """
    cache_dir = Path(cache_dir)

    # --- Priority 1: inline domain map ---
    if raw.get("domain_map"):
        dm = raw["domain_map"]
        if isinstance(dm, str):
            # Path to a JSON file
            try:
                with open(dm) as f:
                    dm = json.load(f)
            except Exception as e:
                raise RuntimeError(f"Could not load domain map from {dm!r}: {e}") from e
        return normalize_domain_map(dm)

    # --- Priority 2: Wikipedia URL ---
    if raw.get("wikipedia_url"):
        url = raw["wikipedia_url"]
        cache_key = _cache_key_for_url(url)
        cache_file = cache_dir / f"{cache_key}.json"

        cached = _load_from_cache(cache_file)
        if cached and _is_enriched(cached):
            print(f"  [domain-resolver] Wikipedia cache hit: {cache_key}", file=sys.stderr)
            return cached

        print(f"  [domain-resolver] Fetching Wikipedia article: {url}", file=sys.stderr)
        article_text = _fetch_wikipedia_text(url)
        dm = _generate_and_enrich(article_text, client, skip_enrich=skip_enrich,
                                   target_concepts_hint=target_concepts_hint)
        _save_to_cache(cache_file, dm)
        return dm

    # --- Priority 3 (default): topic string ---
    topic = raw.get("topic", "")
    if not topic:
        raise ValueError("Raw transcript has no domain map source: provide 'domain_map', 'wikipedia_url', or 'topic'")

    cache_key = _derive_slug(topic)
    # Non-default hint or skip_enrich changes the generated map — use a separate cache slot
    # so slim maps don't collide with full webapp maps for the same topic.
    if skip_enrich or target_concepts_hint is not None:
        cache_key += "-slim"
    cache_file = cache_dir / f"{cache_key}.json"

    cached = _load_from_cache(cache_file)
    if cached:
        if _is_enriched(cached) or skip_enrich:
            print(f"  [domain-resolver] Topic cache hit: {cache_key}", file=sys.stderr)
            return cached
        # Cache exists but lacks enrichment — enrich and re-save
        print(f"  [domain-resolver] Enriching cached domain map for: {topic!r}", file=sys.stderr)
        from tutor_eval.tutors.socratic import enrich_domain_map
        dm = enrich_domain_map(cached, client)
        _save_to_cache(cache_file, dm)
        return dm

    print(f"  [domain-resolver] Generating domain map for topic: {topic!r}", file=sys.stderr)
    dm = _generate_and_enrich(topic, client, skip_enrich=skip_enrich,
                               target_concepts_hint=target_concepts_hint)
    _save_to_cache(cache_file, dm)
    return dm
