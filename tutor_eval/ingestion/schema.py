"""
tutor_eval/ingestion/schema.py

Validation for raw-transcript-v1 JSON files.
"""

from __future__ import annotations

_VALID_ROLES = {"tutor", "student"}


def validate_raw_transcript(data: dict) -> tuple[list[str], list[str]]:
    """
    Validate a raw transcript dict.

    Returns (errors, warnings).
    errors   — must be fixed before ingestion can proceed
    warnings — informational; ingestion continues but results may be unreliable
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(data, dict):
        errors.append("transcript must be a JSON object")
        return errors, warnings

    # --- Required fields ---
    if not data.get("topic"):
        errors.append("'topic' is required")

    turns = data.get("turns")
    if not turns:
        errors.append("'turns' is required and must be non-empty")
    elif not isinstance(turns, list):
        errors.append("'turns' must be a list")
    else:
        for i, t in enumerate(turns):
            if not isinstance(t, dict):
                errors.append(f"turn {i}: must be an object with 'role' and 'content'")
                continue
            if t.get("role") not in _VALID_ROLES:
                errors.append(
                    f"turn {i}: 'role' must be 'tutor' or 'student', got {t.get('role')!r}"
                )
            if not t.get("content"):
                errors.append(f"turn {i}: 'content' is required")

    # --- Domain map source ---
    has_inline = bool(data.get("domain_map"))
    has_url = bool(data.get("wikipedia_url"))
    if not has_inline and not has_url and not data.get("topic"):
        errors.append(
            "no domain map source: provide 'domain_map', 'wikipedia_url', or 'topic'"
        )

    # --- Warnings ---
    if isinstance(turns, list):
        tutor_turns = sum(1 for t in turns if isinstance(t, dict) and t.get("role") == "tutor")
        if tutor_turns < 8:
            warnings.append(
                f"only {tutor_turns} tutor turn(s) — evaluation will be marked is_valid=False "
                f"(minimum 8 required for reliable scores)"
            )

    preset = data.get("bkt_preset")
    if preset and preset not in ("absent", "prereqs_mastered", "all_partial"):
        warnings.append(
            f"unknown bkt_preset {preset!r} — defaulting to 'absent'. "
            f"Valid values: 'absent', 'prereqs_mastered', 'all_partial'"
        )

    return errors, warnings
