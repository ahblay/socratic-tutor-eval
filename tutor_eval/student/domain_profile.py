"""
tutor_eval/student/domain_profile.py

Generate StudentAgent profiles from a domain map.

A "profile" is the dict that StudentAgent.__init__() accepts:
  {mastered: [kc_id, ...], partial: [...], absent: [...],
   misconceptions: [{kc: kc_id, description: str}, ...],
   base_model: "haiku"|"sonnet"}

KC IDs are derived with _derive_slug(), which must match the slugification
in tutor_eval/ingestion/converter.py exactly.

Presets
-------
novice              Root KCs mastered; all others absent.
partial_knowledge   Root KCs + first half of sequence mastered;
                    boundary KC partial; rest absent.
expert              All KCs mastered.
misconception_heavy Root KCs mastered; first 2 non-root partial;
                    rest absent. Pairs well with misconception_count > 0.
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Slug — must match converter._derive_slug exactly
# ---------------------------------------------------------------------------

def _derive_slug(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")[:64]


# ---------------------------------------------------------------------------
# KG builder (mirrors analyzer._build_kg)
# ---------------------------------------------------------------------------

def build_kg_from_domain_map(domain_map: dict) -> dict:
    """
    Convert a normalized domain map to the {kcs, edges} format
    that StudentAgent.build_knowledge_document() expects.
    """
    concepts = domain_map.get("core_concepts", [])
    name_to_slug = {
        c["concept"]: _derive_slug(c["concept"])
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


# ---------------------------------------------------------------------------
# Misconception matching
# ---------------------------------------------------------------------------

def _match_misconception_to_kc(misconception_text: str, kcs: list[dict]) -> str:
    """
    Match a misconception description to the most relevant KC by word overlap.
    Falls back to the first KC if no match.
    """
    if not kcs:
        return "unknown"

    def clean(s: str) -> set[str]:
        return set(re.sub(r"[^a-z0-9\s]", "", s.lower()).split())

    text_words = clean(misconception_text)
    best_id = kcs[0]["id"]
    best_score = -1

    for kc in kcs:
        kc_words = clean(kc["name"])
        score = len(kc_words & text_words)
        # Bonus if full KC name appears as a substring
        if kc["name"].lower() in misconception_text.lower():
            score += len(kc_words) + 1
        if score > best_score:
            best_score = score
            best_id = kc["id"]

    return best_id


# ---------------------------------------------------------------------------
# Preset distributions
# ---------------------------------------------------------------------------

_VALID_PRESETS = {"novice", "partial_knowledge", "expert", "misconception_heavy"}


def generate_profile(
    domain_map: dict,
    preset: str,
    misconception_count: int = 0,
    base_model: str = "haiku",
) -> tuple[dict, dict]:
    """
    Generate a StudentAgent profile and KG from a domain map.

    Parameters
    ----------
    domain_map : dict
        Normalized domain map (core_concepts format).
    preset : str
        One of: novice, partial_knowledge, expert, misconception_heavy.
    misconception_count : int
        Number of misconceptions to inject from domain_map.common_misconceptions.
        0 = none (default). Applies to any preset.
    base_model : str
        StudentAgent model: "haiku" (default) or "sonnet".

    Returns
    -------
    (profile_dict, kg_dict)
        profile_dict is ready to pass to StudentAgent(profile, kg).
        kg_dict is the {kcs, edges} KG.
    """
    if preset not in _VALID_PRESETS:
        raise ValueError(
            f"Unknown preset {preset!r}. Valid: {', '.join(sorted(_VALID_PRESETS))}"
        )

    kg = build_kg_from_domain_map(domain_map)
    kcs = kg["kcs"]
    edges = kg["edges"]

    if not kcs:
        return {
            "mastered": [], "partial": [], "absent": [],
            "misconceptions": [], "base_model": base_model,
        }, kg

    # Identify root KCs (no incoming edges in the prerequisite graph)
    has_incoming: set[str] = {edge["to"] for edge in edges}
    root_ids = [kc["id"] for kc in kcs if kc["id"] not in has_incoming]
    non_root_ids = [kc["id"] for kc in kcs if kc["id"] in has_incoming]

    # Order non-root KCs by recommended_sequence
    name_to_slug = {kc["name"]: kc["id"] for kc in kcs}
    seq_names = domain_map.get("recommended_sequence", [])
    seq_slugs = [name_to_slug[n] for n in seq_names if n in name_to_slug]
    # Append any non-root KCs not in the sequence
    seq_set = set(seq_slugs)
    for kc_id in non_root_ids:
        if kc_id not in seq_set:
            seq_slugs.append(kc_id)
    non_root_ordered = [s for s in seq_slugs if s in set(non_root_ids)]

    # --- Apply preset ---
    if preset == "novice":
        mastered: list[str] = []
        partial: list[str] = list(root_ids)
        absent = non_root_ordered

    elif preset == "partial_knowledge":
        mid = max(1, len(non_root_ordered) // 2)
        mastered = list(root_ids) + non_root_ordered[:mid]
        partial = [non_root_ordered[mid]] if mid < len(non_root_ordered) else []
        absent = non_root_ordered[mid + 1:] if mid + 1 < len(non_root_ordered) else []

    elif preset == "expert":
        mastered = [kc["id"] for kc in kcs]
        partial = []
        absent = []

    else:  # misconception_heavy
        mastered = list(root_ids)
        partial = non_root_ordered[:2]
        absent = non_root_ordered[2:]

    # --- Inject misconceptions ---
    misconceptions: list[dict] = []
    if misconception_count > 0:
        raw = domain_map.get("common_misconceptions", [])
        for i, m in enumerate(raw):
            if i >= misconception_count:
                break
            if isinstance(m, str):
                desc = m
            elif isinstance(m, dict):
                desc = (
                    m.get("misconception")
                    or m.get("description")
                    or str(m)
                )
            else:
                continue
            kc_id = _match_misconception_to_kc(desc, kcs)
            misconceptions.append({"kc": kc_id, "description": desc})

    profile = {
        "mastered": mastered,
        "partial": partial,
        "absent": absent,
        "misconceptions": misconceptions,
        "base_model": base_model,
    }
    return profile, kg


# ---------------------------------------------------------------------------
# BKT initial states from profile
# ---------------------------------------------------------------------------

def bkt_states_from_profile(profile: dict, kg: dict) -> dict:
    """
    Convert a student profile to bkt_initial_states format for raw-transcript-v1.
    Mastered → p=0.90, partial → p=0.50, absent → p=0.10.
    """
    mastered_set = set(profile.get("mastered", []))
    partial_set = set(profile.get("partial", []))
    states: dict[str, dict] = {}
    for kc in kg.get("kcs", []):
        kc_id = kc["id"]
        if kc_id in mastered_set:
            states[kc_id] = {
                "p_mastered": 0.90, "knowledge_class": "mastered",
                "observation_history": [],
            }
        elif kc_id in partial_set:
            states[kc_id] = {
                "p_mastered": 0.50, "knowledge_class": "partial",
                "observation_history": [],
            }
        else:
            states[kc_id] = {
                "p_mastered": 0.10, "knowledge_class": "absent",
                "observation_history": [],
            }
    return states
