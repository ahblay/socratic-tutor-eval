"""
convolearn/parse.py

Stage 1: Load the ConvoLearn HuggingFace dataset, group by first Student
utterance, filter qualifying prompts, and return a reproducible sample.
"""

from __future__ import annotations

import random
import re


def _extract_first_student(conversation: str) -> str:
    """Return the first Student utterance from a cleaned_conversation string."""
    for line in conversation.splitlines():
        stripped = line.strip()
        if stripped.startswith("Student:"):
            return stripped[len("Student:"):].strip()
    return ""


def _derive_slug(s: str) -> str:
    slug = s.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:80]


def load_and_sample(
    dataset_name: str = "masharma/convolearn",
    min_dialogues: int = 20,
    min_exchanges: int = 10,
    sample_size: int = 7,
    seed: int = 42,
) -> list[dict]:
    """
    Load dataset, group by first Student utterance, filter, and sample.

    Returns a list of prompt-group dicts matching sampled_dialogues.json schema.
    """
    from datasets import load_dataset

    print(f"[parse] Loading dataset {dataset_name!r} ...", flush=True)
    ds = load_dataset(dataset_name, split="train")

    # Group qualifying rows by first Student utterance
    groups: dict[str, dict] = {}
    for row in ds:
        qp = _extract_first_student(row["cleaned_conversation"])
        if not qp:
            continue
        if qp not in groups:
            groups[qp] = {
                "earthscience_topic": row.get("earthscience_topic", ""),
                "rows": [],
            }
        if row.get("num_exchanges", 0) >= min_exchanges:
            groups[qp]["rows"].append(dict(row))

    # Filter: must have >= min_dialogues qualifying rows
    qualifying: list[tuple[str, dict]] = [
        (qp, data)
        for qp, data in groups.items()
        if len(data["rows"]) >= min_dialogues
    ]
    print(
        f"[parse] {len(qualifying)} prompts meet criteria "
        f"(>={min_dialogues} dialogues with >={min_exchanges} exchanges)",
        flush=True,
    )

    # Reproducible sample
    rng = random.Random(seed)
    if len(qualifying) > sample_size:
        qualifying = rng.sample(qualifying, sample_size)

    print(f"[parse] Sampled {len(qualifying)} prompts", flush=True)

    result: list[dict] = []
    for qp, data in qualifying:
        dialogues = [
            {
                "dialogue_idx": idx,
                "cleaned_conversation": row["cleaned_conversation"],
                "effectiveness_consensus": row.get("effectiveness_consensus"),
                "completeness_consensus": row.get("completeness_consensus"),
                "num_exchanges": row.get("num_exchanges"),
            }
            for idx, row in enumerate(data["rows"])
        ]
        result.append({
            "prompt_id": _derive_slug(qp),
            "question_prompt": qp,
            "earthscience_topic": data["earthscience_topic"],
            "dialogues": dialogues,
        })

    return result
