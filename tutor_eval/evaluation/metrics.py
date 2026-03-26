"""
tutor_eval/evaluation/metrics.py

Scoring functions operating on List[TurnResult].
No LLM calls — all per-turn classifications are pre-computed in analyzer.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tutor_eval.evaluation.analyzer import TurnResult


def compute_nac(turn_results: list[TurnResult]) -> float:
    """
    Non-Answer Compliance: fraction of turns classified compliant.
    Disabled turns are excluded from both numerator and denominator.
    Returns 1.0 when all turns are disabled (compute_nac=False globally).
    """
    verdicts = [
        tr.nac_verdict for tr in turn_results
        if tr.nac_verdict != "disabled"
    ]
    if not verdicts:
        return 1.0
    return sum(1 for v in verdicts if v == "compliant") / len(verdicts)


def compute_kft(turn_results: list[TurnResult]) -> float:
    """
    Knowledge Frontier Targeting: fraction of turns targeting a frontier KC.
    Turns with no identifiable KC (kc_status=None) are treated as off_map (0.0),
    not excluded — the tutor turn still happened and consumed session time.
    """
    if not turn_results:
        return 0.0
    hits = sum(1 for tr in turn_results if tr.kc_status == "on_frontier")
    return hits / len(turn_results)


def compute_pr(turn_results: list[TurnResult]) -> float:
    """
    Progression Rate: 1 minus the fraction of turns spent in a stall.
    Stall turns are pre-computed by _detect_stalls() in analyzer.py.
    """
    if not turn_results:
        return 1.0
    stall_turns = sum(1 for tr in turn_results if tr.is_stall_turn)
    return 1.0 - (stall_turns / len(turn_results))


def compute_lcq(turn_results: list[TurnResult]) -> float:
    """
    Lesson Calibration Quality: fraction of turns where observed response type
    matched the warranted type.
    Turns where either classification is None (classifier failed) are excluded.
    """
    classifiable = [
        tr for tr in turn_results
        if tr.observed_type is not None and tr.warranted_type is not None
    ]
    if not classifiable:
        return 0.0
    aligned = sum(1 for tr in classifiable if tr.observed_type == tr.warranted_type)
    return aligned / len(classifiable)


def compute_mrq(turn_results: list[TurnResult]) -> float | None:
    """
    Misconception Response Quality: fraction of misconception turns where the
    tutor probed Socratically.
    Returns None when no misconceptions were detected in the session.
    """
    misconception_turns = [
        tr for tr in turn_results
        if tr.mrq_verdict in ("probed", "ignored")
    ]
    if not misconception_turns:
        return None
    probed = sum(1 for tr in misconception_turns if tr.mrq_verdict == "probed")
    return probed / len(misconception_turns)


def compute_composite(
    nac: float,
    kft: float,
    pr: float,
    lcq: float,
    mrq_adjustment: float,
) -> float:
    """
    Session composite: NAC × (0.5·KFT + 0.25·PR + 0.25·LCQ + MRQ_adjustment)

    MRQ_adjustment = 0.15 × (MRQ − 0.5) when misconceptions present, else 0.0.
    Scores above 1.0 are valid (strong misconception handling can reach ~1.075).
    """
    return nac * (0.5 * kft + 0.25 * pr + 0.25 * lcq + mrq_adjustment)
