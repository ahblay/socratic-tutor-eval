"""
tutor_eval/evaluation/metrics.py

Scoring functions for the tutor evaluation framework.
"""

from __future__ import annotations

import json
import re
import sys

import anthropic


# ---------------------------------------------------------------------------
# KFT — Knowledge Frontier Targeting
# ---------------------------------------------------------------------------

_KFT_PROMPT = """\
You are an educational assessment system. Given a tutor's question and the \
current knowledge frontier (KCs not yet mastered but with prerequisites met), \
classify whether the tutor's question targets a KC on the frontier.

KNOWLEDGE FRONTIER (KC IDs on the frontier):
{frontier}

TUTOR QUESTION:
{tutor_question}

ALL KCs for reference:
{kc_listing}

Answer with ONLY a JSON object:
{{
  "targeted_kc": "<KC ID from the frontier that the question targets, or null>",
  "on_frontier": true/false,
  "reasoning": "one sentence"
}}
Do not include any text outside the JSON object."""


def compute_kft(
    log: list[dict],
    kg: dict,
    client: anthropic.Anthropic,
) -> float:
    """
    Knowledge Frontier Targeting: for each turn, use LLM (haiku) to classify
    which KC the tutor's question targets, and check if it is on the frontier.
    Returns the fraction of turns where the tutor targeted the frontier.
    """
    if not log:
        return 0.0

    kc_listing = "\n".join(
        f"  {kc['id']}: {kc['name']}" for kc in kg.get("kcs", [])
    )

    hits = 0
    total = 0

    for entry in log:
        tutor_q = entry.get("tutor_response", "")
        snap    = entry.get("evaluator_snapshot", {})
        frontier = snap.get("knowledge_frontier", [])

        if not tutor_q or not frontier:
            continue

        total += 1
        prompt = _KFT_PROMPT.format(
            frontier=", ".join(frontier),
            tutor_question=tutor_q[:500],
            kc_listing=kc_listing,
        )

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
            if data.get("on_frontier"):
                hits += 1
        except Exception as e:
            print(f"  [KFT] classifier error: {e}", file=sys.stderr)

    return hits / total if total > 0 else 0.0


# ---------------------------------------------------------------------------
# MRQ — Misconception Response Quality
# ---------------------------------------------------------------------------

_MRQ_PROMPT = """\
A student expressed a misconception during a tutoring session. Evaluate whether \
the tutor's response was a Socratic probe (good) or a direct correction (bad).

STUDENT MESSAGE (containing misconception):
{student_message}

TUTOR RESPONSE:
{tutor_response}

MISCONCEPTION observed (from evaluator):
{misconception_info}

Answer with ONLY a JSON object:
{{
  "handled_socratically": true/false,
  "reasoning": "one sentence"
}}
true = tutor asked a question that would lead the student to discover the error.
false = tutor directly stated the correction.
Do not include any text outside the JSON object."""


def compute_mrq(
    log: list[dict],
    client: anthropic.Anthropic,
) -> float:
    """
    Misconception Response Quality: for turns where the student expressed a
    misconception, did the tutor respond with a Socratic probe?
    Returns fraction of misconception turns handled well.
    """
    if not log:
        return 0.0

    hits  = 0
    total = 0

    for entry in log:
        snap = entry.get("evaluator_snapshot", {})
        obs  = snap.get("observations", [])

        misconceptions = [
            o for o in obs if o.get("observation_class") == "misconception"
        ]
        if not misconceptions:
            continue

        total += 1
        student_msg  = entry.get("student_message", "")
        tutor_resp   = entry.get("tutor_response", "")
        misc_info    = "; ".join(
            f"{m['kc_id']}: {m.get('evidence_quote', '')}" for m in misconceptions
        )

        prompt = _MRQ_PROMPT.format(
            student_message=student_msg[:400],
            tutor_response=tutor_resp[:400],
            misconception_info=misc_info,
        )

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
            if data.get("handled_socratically"):
                hits += 1
        except Exception as e:
            print(f"  [MRQ] classifier error: {e}", file=sys.stderr)

    return hits / total if total > 0 else 1.0  # default to 1.0 if no misconceptions


# ---------------------------------------------------------------------------
# RS — Robustness Score
# ---------------------------------------------------------------------------

def compute_rs(logs: list[list[dict]]) -> float:
    """
    Robustness Score: given logs from multiple student profiles, compute
    consistency of KFT across profiles.  Lower variance = higher robustness.

    Each log's KFT is estimated from its entries' evaluator snapshots
    (fraction of turns where frontier is non-empty — a proxy for targeting).
    This avoids requiring extra LLM calls here; use precomputed KFT values
    when available.

    Returns a score in [0, 1] where 1 = perfectly consistent.
    """
    if len(logs) < 2:
        return 1.0

    kft_scores: list[float] = []
    for log in logs:
        if not log:
            kft_scores.append(0.0)
            continue
        # proxy: fraction of turns where frontier is non-empty
        hits  = sum(
            1 for e in log
            if e.get("evaluator_snapshot", {}).get("knowledge_frontier")
        )
        kft_scores.append(hits / len(log))

    mean = sum(kft_scores) / len(kft_scores)
    variance = sum((x - mean) ** 2 for x in kft_scores) / len(kft_scores)
    # Robustness = 1 - normalized variance (variance is in [0, 0.25] for p in [0,1])
    return max(0.0, 1.0 - 4 * variance)


# ---------------------------------------------------------------------------
# TBA — Teacher Belief Accuracy
# ---------------------------------------------------------------------------

def compute_tba(log: list[dict]) -> float | None:
    """
    Teacher Belief Accuracy: compare tutor's internal phase (from
    session_snapshot) with evaluator's estimated_phase.

    Returns the fraction of turns where they agree (within ±1),
    or None if no session_snapshot data is available.
    """
    entries_with_both = [
        e for e in log
        if e.get("session_snapshot") and e.get("evaluator_snapshot")
    ]
    if not entries_with_both:
        return None

    hits  = 0
    total = len(entries_with_both)

    for entry in entries_with_both:
        tutor_phase    = entry["session_snapshot"].get("current_phase", 1)
        eval_phase_idx = entry["evaluator_snapshot"].get("estimated_phase", -1)
        # estimated_phase is 0-based index of furthest mastered KC
        # Convert to 1-based rough phase: idx + 2 capped at 6
        eval_phase = min(6, max(1, eval_phase_idx + 2))

        if abs(tutor_phase - eval_phase) <= 1:
            hits += 1

    return hits / total if total > 0 else None


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

def compute_composite(
    kft: float,
    mrq: float,
    rs: float,
    tba: float | None = None,
) -> float:
    """
    Weighted composite score.

    Default weights: KFT 0.4, MRQ 0.3, RS 0.2, TBA 0.1
    If TBA is None, redistribute its weight proportionally to the others.
    """
    if tba is not None:
        return 0.4 * kft + 0.3 * mrq + 0.2 * rs + 0.1 * tba
    else:
        # Redistribute TBA weight (0.1) proportionally: 0.4/0.9, 0.3/0.9, 0.2/0.9
        total = 0.9
        return (0.4 / total) * kft + (0.3 / total) * mrq + (0.2 / total) * rs
