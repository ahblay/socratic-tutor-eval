"""
convolearn/adapter.py

Stage 3: Convert ConvoLearn dialogue dicts into the analysis_input format
expected by analyze_transcript(). Thin wrapper over tutor_eval/ingestion/.
"""

from __future__ import annotations

import re

from tutor_eval.ingestion.converter import prepare_analysis_input


def _parse_conversation(text: str) -> list[dict]:
    """
    Parse a cleaned_conversation string into a list of {role, content} dicts.

    Role labels "Student:" and "Teacher:" start a new turn; content accumulates
    across continuation lines until the next label (handles multi-line responses).
    Roles are lowercased here; converter.py normalises student→user, teacher→tutor.
    """
    turns: list[dict] = []
    current_role: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r"^(Student|Teacher):\s*(.*)", stripped)
        if m:
            # Flush previous turn
            if current_role is not None:
                content = " ".join(current_lines).strip()
                if content:
                    turns.append({"role": current_role, "content": content})
            current_role = m.group(1).lower()  # "student" | "teacher"
            first_line = m.group(2).strip()
            current_lines = [first_line] if first_line else []
        elif current_role is not None and stripped:
            current_lines.append(stripped)

    # Flush last turn
    if current_role is not None:
        content = " ".join(current_lines).strip()
        if content:
            turns.append({"role": current_role, "content": content})

    return turns


def _flatten_domain_map(domain_map: dict) -> dict:
    """Strip prerequisite_for edges so all KCs are always frontier-eligible."""
    flat = [
        {**c, "prerequisite_for": []}
        for c in domain_map.get("core_concepts", [])
    ]
    return {**domain_map, "core_concepts": flat}


def adapt_dialogue(
    prompt_id: str,
    question_prompt: str,
    dialogue: dict,
    domain_map: dict,
    bkt_preset: str = "absent",
) -> dict:
    """
    Convert one ConvoLearn dialogue into an analysis_input dict.

    session_id: "{prompt_id}_{dialogue_idx}"
    bkt_preset: "absent" (tabula_rasa default), "prereqs_mastered", or "all_partial".
    """
    session_id = f"{prompt_id}_{dialogue['dialogue_idx']}"
    turns = _parse_conversation(dialogue["cleaned_conversation"])

    raw = {
        "session_id": session_id,
        "topic": question_prompt,
        "turns": turns,
        "bkt_preset": bkt_preset,
    }

    return prepare_analysis_input(raw, _flatten_domain_map(domain_map))
