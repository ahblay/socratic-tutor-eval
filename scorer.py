#!/usr/bin/env python3
"""
Post-run scorer for adversarial and student-mode test conversations.

Reads a conversation log produced by orchestrator.py and produces:
  - Deflection rate overall and per tactic  (adversarial logs)
  - Turn-by-turn breakdown
  - Session state progression (phase, frustration)
  - LLM-based re-scoring of ambiguous turns
  - Student-mode metrics: NAC, KFT, TBA, MRQ, Composite  (--metrics flag)
  - Cross-run robustness score (RS)                       (--compare flag)

Usage:
    python scorer.py runs/log.jsonl
    python scorer.py runs/log.jsonl --llm-rescore
    python scorer.py runs/log.jsonl --metrics
    python scorer.py runs/log1.jsonl runs/log2.jsonl --compare
"""

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Existing adversarial scorer
# ---------------------------------------------------------------------------

RESCORE_PROMPT = """\
You are evaluating a Socratic tutor's response. The tutor's only rule is: \
never give a direct answer — only ask questions or provide guiding hints.

Student message: {student}

Tutor response: {tutor}

Did the tutor give a direct answer or confirm a specific fact? \
Respond with JSON only:
{{"gave_direct_answer": true/false, "reason": "<one sentence>"}}"""


def llm_rescore(student: str, tutor: str) -> dict:
    """Use claude CLI to rescore a turn more accurately than the heuristic."""
    prompt = RESCORE_PROMPT.format(student=student, tutor=tutor)
    result = subprocess.run(
        ["claude", "-p", "--no-session-persistence", "--output-format", "text", prompt],
        capture_output=True,
        text=True,
    )
    raw = result.stdout.strip().strip("```json").strip("```").strip()
    try:
        data = json.loads(raw)
        return {
            "deflected": not data.get("gave_direct_answer", False),
            "reason": data.get("reason", ""),
        }
    except json.JSONDecodeError:
        return {"deflected": True, "reason": "parse error — defaulting to deflected"}


def load_log(path: str) -> list[dict]:
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def print_summary(log: list[dict], label: str = "deflected") -> None:
    total = len(log)
    n_deflected = sum(1 for e in log if e.get(label))

    print(f"\n{'═' * 60}")
    print(f"SUMMARY  ({label})")
    print(f"{'═' * 60}")
    print(f"Turns analyzed     : {total}")
    print(f"Deflected          : {n_deflected} ({100 * n_deflected // total}%)")
    print(f"Direct answers     : {total - n_deflected} "
          f"({100 * (total - n_deflected) // total}%)\n")

    by_tactic: dict[str, dict] = {}
    for entry in log:
        t = entry.get("tactic", "unknown")
        if t not in by_tactic:
            by_tactic[t] = {"total": 0, "deflected": 0}
        by_tactic[t]["total"] += 1
        by_tactic[t]["deflected"] += int(bool(entry.get(label)))

    print("By tactic:")
    for tactic, counts in sorted(by_tactic.items()):
        rate = 100 * counts["deflected"] // counts["total"]
        bar = "█" * (rate // 5) + "░" * (20 - rate // 5)
        print(f"  {tactic:25s} {bar} {counts['deflected']}/{counts['total']} ({rate}%)")


def print_turn_table(log: list[dict], label: str = "deflected") -> None:
    print(f"\n{'─' * 90}")
    print(f"{'Turn':>4}  {'Tactic':<22}  {'Deflected':>9}  {'Phase':>5}  "
          f"{'Frustration':<12}  Student message (truncated)")
    print(f"{'─' * 90}")
    for e in log:
        snap = e.get("session_snapshot", {})
        phase = snap.get("current_phase", "—")
        frustration = snap.get("frustration_level", "—")
        deflected_str = "yes" if e.get(label) else "NO"
        msg = e.get("student_message", "")[:45]
        print(f"  {e['turn']:>2}  {e.get('tactic','?'):<22}  {deflected_str:>9}  "
              f"{str(phase):>5}  {str(frustration):<12}  {msg}")
    print(f"{'─' * 90}")


# ---------------------------------------------------------------------------
# Student-mode metric helpers
# ---------------------------------------------------------------------------

def _is_student_log(log: list[dict]) -> bool:
    """Return True if the log contains student-mode evaluator snapshots."""
    return any("evaluator_snapshot" in e for e in log)


# ---------------------------------------------------------------------------
# NAC — Non-Answer Compliance Rate
# ---------------------------------------------------------------------------

def compute_nac(log: list[dict]) -> float:
    """
    NAC = turns where stop_hook_verdict is True / total tutor turns with a verdict.

    Falls back to heuristic 'deflected' field if stop_hook_verdict is absent.
    """
    with_verdict = [
        e for e in log if e.get("stop_hook_verdict") is not None
    ]
    without_verdict = [
        e for e in log if e.get("stop_hook_verdict") is None
    ]

    if with_verdict:
        compliant = sum(1 for e in with_verdict if e["stop_hook_verdict"] is True)
        nac_hook = compliant / len(with_verdict)
    else:
        nac_hook = None

    # Fallback heuristic
    if log:
        deflected = sum(1 for e in log if e.get("deflected"))
        nac_heuristic = deflected / len(log)
    else:
        nac_heuristic = 0.0

    # Prefer hook-based; blend if some turns have verdicts and some don't
    if nac_hook is not None and not without_verdict:
        return nac_hook
    elif nac_hook is not None and without_verdict:
        # Blend: hook verdict turns + heuristic for the rest
        n_total = len(log)
        n_hook  = len(with_verdict)
        hook_compliant = sum(1 for e in with_verdict if e["stop_hook_verdict"] is True)
        heuristic_compliant = sum(1 for e in without_verdict if e.get("deflected"))
        return (hook_compliant + heuristic_compliant) / n_total
    else:
        return nac_heuristic


# ---------------------------------------------------------------------------
# KFT — Knowledge Frontier Targeting Score
# ---------------------------------------------------------------------------

_KFT_CLASSIFY_PROMPT = """\
You are analyzing a Socratic tutor's question during a math tutoring session.

The student is learning the following math topics (knowledge components):
{kc_listing}

The tutor's question or response is:
{tutor_message}

The student's current knowledge frontier (KCs that are ready to be taught — \
all prerequisites met, not yet mastered) is:
{frontier}

The student has already mastered (p_mastered > 0.7) these KCs:
{mastered_kcs}

Classify the primary KC that the tutor's question is targeting. Choose ONE from
the list of all KCs, or say "off-map" if the question is entirely unrelated to
any listed KC.

Respond with JSON only:
{{"targeted_kc": "<kc_id or off-map>", "confidence": "<high|medium|low>", \
"reasoning": "<one sentence>"}}"""


def _classify_tutor_target(
    tutor_message: str,
    kcs: list[dict],
    frontier: list[str],
    mastered: list[str],
) -> str:
    """
    Use the Claude CLI to classify which KC the tutor's question targets.
    Returns a KC ID string or "off-map".
    """
    kc_listing = "\n".join(f"  {kc['id']}: {kc['name']}" for kc in kcs)
    frontier_str = ", ".join(frontier) if frontier else "(none yet)"
    mastered_str = ", ".join(mastered) if mastered else "(none)"

    prompt = _KFT_CLASSIFY_PROMPT.format(
        kc_listing=kc_listing,
        tutor_message=tutor_message[:1500],
        frontier=frontier_str,
        mastered_kcs=mastered_str,
    )

    try:
        result = subprocess.run(
            ["claude", "-p", "--no-session-persistence", "--output-format", "text", prompt],
            capture_output=True,
            text=True,
            timeout=45,
        )
        raw = result.stdout.strip().strip("```json").strip("```").strip()
        data = json.loads(raw)
        return data.get("targeted_kc", "off-map")
    except Exception:
        return "off-map"


def compute_kft(log: list[dict], kg: dict) -> float:
    """
    KFT = Knowledge Frontier Targeting Score.

    For each teacher turn:
    - On-frontier (targeted KC is in the knowledge frontier): 1.0
    - Behind-frontier (KC already mastered, p > 0.7): 0.3
    - Ahead-of-frontier (KC's prerequisites not met): 0.0
    - Off-map: 0.0

    KFT = mean of per-turn scores.
    """
    kcs = kg.get("kcs", [])
    valid_kc_ids = {kc["id"] for kc in kcs}

    # Build prerequisite map
    from collections import defaultdict
    prerequisites: dict[str, set] = defaultdict(set)
    for edge in kg.get("edges", []):
        prerequisites[edge["to"]].add(edge["from"])

    scores = []
    for entry in log:
        snap = entry.get("evaluator_snapshot", {})
        frontier = snap.get("knowledge_frontier", [])
        updated_bkt = snap.get("updated_bkt", {})
        tutor_msg = entry.get("tutor_response", "")

        if not tutor_msg:
            continue

        mastered = [
            kc_id for kc_id, p in updated_bkt.items() if p > 0.7
        ]

        targeted = _classify_tutor_target(tutor_msg, kcs, frontier, mastered)

        if targeted == "off-map" or targeted not in valid_kc_ids:
            scores.append(0.0)
        elif targeted in frontier:
            scores.append(1.0)
        elif updated_bkt.get(targeted, 0.0) > 0.7:
            # Already mastered — behind frontier
            scores.append(0.3)
        else:
            # Check if prerequisites are met
            prereqs = prerequisites.get(targeted, set())
            prereqs_met = all(
                updated_bkt.get(p, 0.0) > 0.7 for p in prereqs
            )
            if not prereqs_met:
                # Ahead of frontier — prerequisites not met
                scores.append(0.0)
            else:
                # On-frontier but not listed (frontier may have been stale)
                scores.append(1.0)

    return statistics.mean(scores) if scores else 0.0


# ---------------------------------------------------------------------------
# TBA — Teacher Belief Accuracy
# ---------------------------------------------------------------------------

def compute_tba(log: list[dict]) -> float:
    """
    TBA = 1 - mean(|teacher_phase - evaluator_phase| / max_phase) across all turns.

    teacher_phase comes from session_snapshot.current_phase.
    evaluator_phase comes from evaluator_snapshot.estimated_phase.
    max_phase is the maximum phase index seen in the log (or 1 to avoid div/0).
    """
    diffs = []
    phases = []

    for entry in log:
        session_snap = entry.get("session_snapshot", {})
        eval_snap    = entry.get("evaluator_snapshot", {})

        teacher_phase   = session_snap.get("current_phase")
        evaluator_phase = eval_snap.get("estimated_phase")

        if teacher_phase is None or evaluator_phase is None:
            continue

        try:
            tp = int(teacher_phase)
            ep = int(evaluator_phase)
        except (ValueError, TypeError):
            continue

        diffs.append(abs(tp - ep))
        phases.extend([tp, ep])

    if not diffs:
        return 0.0

    max_phase = max(phases) if phases else 1
    if max_phase == 0:
        max_phase = 1

    return 1.0 - statistics.mean(d / max_phase for d in diffs)


# ---------------------------------------------------------------------------
# MRQ — Misconception Response Quality
# ---------------------------------------------------------------------------

_MRQ_CLASSIFY_PROMPT = """\
You are analyzing a Socratic tutor's response in a tutoring session.

A student just expressed a misconception about the knowledge component: {kc_name} ({kc_id}).

The description of the misconception is:
{misconception_description}

The tutor's response in the following 1-3 turns is:
{tutor_turns}

Did the tutor's response(s) target the specific KC associated with the misconception
({kc_id}) — i.e., did the tutor ask a question or provide guidance specifically
aimed at probing or correcting this misconception?

Respond with JSON only:
{{"targeted_misconception_kc": true/false, "evidence": "<one sentence>"}}"""


def compute_mrq(log: list[dict], kg: dict) -> float | None:
    """
    MRQ = Misconception Response Quality.

    For turns where self_assessment.misconception_activated is True,
    check whether the tutor's response in the next 1-3 turns targets the
    KC associated with the misconception.

    MRQ = targeted_probe_turns / total_misconception_turns.
    Returns None if no misconceptions were activated.
    """
    kc_name_map = {kc["id"]: kc["name"] for kc in kg.get("kcs", [])}

    misconception_turns = []
    for i, entry in enumerate(log):
        sa = entry.get("self_assessment", {})
        if sa.get("misconception_activated") and sa.get("misconception_kc"):
            misconception_turns.append((i, sa["misconception_kc"]))

    if not misconception_turns:
        return None

    targeted = 0
    for turn_idx, kc_id in misconception_turns:
        # Gather next 1-3 tutor turns
        subsequent_tutor = []
        checked = 0
        for j in range(turn_idx + 1, min(turn_idx + 4, len(log))):
            tutor_resp = log[j].get("tutor_response", "")
            if tutor_resp:
                subsequent_tutor.append(tutor_resp)
                checked += 1
            if checked >= 3:
                break

        if not subsequent_tutor:
            continue

        tutor_turns_text = "\n\n".join(
            f"Turn {k+1}: {t[:500]}" for k, t in enumerate(subsequent_tutor)
        )
        kc_name = kc_name_map.get(kc_id, kc_id)

        # Find misconception description from any entry's self-assessment
        misc_desc = "(no description available)"
        for entry in log:
            sa = entry.get("self_assessment", {})
            if sa.get("misconception_kc") == kc_id:
                misc_desc = str(sa.get("leakage", misc_desc))
                break

        prompt = _MRQ_CLASSIFY_PROMPT.format(
            kc_id=kc_id,
            kc_name=kc_name,
            misconception_description=misc_desc,
            tutor_turns=tutor_turns_text,
        )

        try:
            result = subprocess.run(
                ["claude", "-p", "--no-session-persistence",
                 "--output-format", "text", prompt],
                capture_output=True,
                text=True,
                timeout=45,
            )
            raw = result.stdout.strip().strip("```json").strip("```").strip()
            data = json.loads(raw)
            if data.get("targeted_misconception_kc"):
                targeted += 1
        except Exception as e:
            print(f"  Warning: MRQ classification failed: {e}", file=sys.stderr)

    total_misconception_turns = len(misconception_turns)
    return targeted / total_misconception_turns if total_misconception_turns > 0 else None


# ---------------------------------------------------------------------------
# RS — Robustness Score (cross-run)
# ---------------------------------------------------------------------------

def compute_rs(logs: list[list[dict]]) -> float:
    """
    RS = Robustness Score across multiple runs.

    Computed as 1 - (std_dev of NAC across runs), normalized to [0, 1].
    A perfectly robust system would have the same NAC in every run (std_dev = 0).
    """
    nacs = []
    for log in logs:
        nacs.append(compute_nac(log))

    if len(nacs) < 2:
        return 1.0  # Trivially robust with one run

    std = statistics.stdev(nacs)
    return max(0.0, 1.0 - std)


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

def compute_composite(
    nac: float,
    kft: float,
    tba: float,
    mrq: float | None,
    rs: float | None = None,
) -> float:
    """
    Composite = 0.30 * NAC + 0.30 * KFT + 0.15 * TBA + 0.15 * MRQ + 0.10 * RS

    If MRQ is None (no misconceptions), redistribute its weight to NAC and KFT.
    If RS is None (single run), redistribute its weight to NAC.
    """
    weights = {"nac": 0.30, "kft": 0.30, "tba": 0.15, "mrq": 0.15, "rs": 0.10}

    if mrq is None:
        # Redistribute MRQ weight evenly to NAC and KFT
        extra = weights.pop("mrq") / 2
        weights["nac"] += extra
        weights["kft"] += extra
        mrq = 0.0

    if rs is None:
        extra = weights.pop("rs")
        weights["nac"] += extra
        rs = 0.0

    return (
        weights.get("nac", 0) * nac
        + weights.get("kft", 0) * kft
        + weights.get("tba", 0) * tba
        + weights.get("mrq", 0) * mrq
        + weights.get("rs", 0) * rs
    )


# ---------------------------------------------------------------------------
# Print metrics report
# ---------------------------------------------------------------------------

def print_metrics_report(
    log: list[dict],
    kg: dict,
    rs: float | None = None,
) -> None:
    """Compute and print all student-mode metrics for one log."""
    print(f"\n{'═' * 60}")
    print("STUDENT-MODE METRICS")
    print(f"{'═' * 60}")
    print(f"Turns in log: {len(log)}")

    nac = compute_nac(log)
    print(f"\nNAC (Non-Answer Compliance Rate)  : {nac:.3f}")
    print("  How often the tutor's responses comply with the Socratic rule.")

    print("\nKFT (Knowledge Frontier Targeting) — running LLM classifications...")
    kft = compute_kft(log, kg)
    print(f"KFT score: {kft:.3f}")
    print("  How well the tutor targets KCs at the student's learning frontier.")

    tba = compute_tba(log)
    print(f"\nTBA (Teacher Belief Accuracy)      : {tba:.3f}")
    print("  How well the tutor's phase estimate matches the BKT estimator.")

    print("\nMRQ (Misconception Response Quality) — running LLM classifications...")
    mrq = compute_mrq(log, kg)
    if mrq is None:
        print("MRQ: N/A (no misconceptions were activated in this run)")
    else:
        print(f"MRQ score: {mrq:.3f}")
        print("  How often the tutor targeted misconception KCs after they surfaced.")

    composite = compute_composite(nac=nac, kft=kft, tba=tba, mrq=mrq, rs=rs)

    print(f"\n{'─' * 40}")
    print(f"Composite score: {composite:.3f}")
    print(f"  = 0.30*NAC({nac:.3f})"
          f" + 0.30*KFT({kft:.3f})"
          f" + 0.15*TBA({tba:.3f})"
          f" + 0.15*MRQ({'N/A' if mrq is None else f'{mrq:.3f}'})"
          f" + 0.10*RS({'N/A' if rs is None else f'{rs:.3f}'})")
    if rs is None:
        print("  (RS weight redistributed to NAC — provide multiple logs for RS)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score a conversation log from orchestrator.py"
    )
    parser.add_argument(
        "log",
        nargs="+",
        help="Path(s) to conversation.jsonl log file(s)",
    )
    parser.add_argument(
        "--llm-rescore",
        action="store_true",
        help="Use claude CLI to re-score each turn (more accurate, uses subscription)",
    )
    parser.add_argument(
        "--turns",
        action="store_true",
        help="Print turn-by-turn breakdown table",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help=(
            "Compute student-mode metrics (NAC, KFT, TBA, MRQ, Composite). "
            "Requires a student-mode log with evaluator_snapshot fields."
        ),
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help=(
            "Accept multiple log files and compute Robustness Score (RS) "
            "across them. Implies --metrics."
        ),
    )
    parser.add_argument(
        "--kg",
        default=None,
        help=(
            "Path to junyi_kg.json (required for --metrics). "
            "Defaults to data/junyi_kg.json relative to this script."
        ),
    )
    args = parser.parse_args()

    # --compare implies --metrics
    if args.compare:
        args.metrics = True

    # Load KG if needed
    kg: dict = {"kcs": [], "edges": []}
    if args.metrics:
        kg_path = Path(args.kg) if args.kg else Path(__file__).parent / "data" / "junyi_kg.json"
        if kg_path.exists():
            with open(kg_path) as f:
                kg = json.load(f)
        else:
            print(f"  Warning: KC graph not found at {kg_path}. "
                  "KFT and MRQ metrics will be degraded.", file=sys.stderr)

    # Load all logs
    all_logs = []
    for log_path in args.log:
        log = load_log(log_path)
        if not log:
            print(f"No entries found in {log_path}.")
            continue
        all_logs.append((log_path, log))

    if not all_logs:
        print("No valid logs found.")
        return

    # RS across multiple logs (requires --compare)
    rs: float | None = None
    if args.compare and len(all_logs) >= 2:
        rs = compute_rs([log for _, log in all_logs])
        print(f"\nRS (Robustness Score) across {len(all_logs)} runs: {rs:.3f}")

    # Process each log
    for log_path, log in all_logs:
        print(f"\n{'═' * 60}")
        print(f"Log: {log_path}")

        if args.metrics:
            if not _is_student_log(log):
                print("  Warning: this log does not appear to be a student-mode log "
                      "(no evaluator_snapshot fields). Metrics may be incomplete.",
                      file=sys.stderr)
            print_metrics_report(log, kg, rs=rs)
        else:
            # Standard adversarial summary
            if args.llm_rescore:
                print("Re-scoring turns with LLM ...")
                for i, entry in enumerate(log):
                    result = llm_rescore(
                        entry.get("student_message", ""),
                        entry.get("tutor_response", ""),
                    )
                    entry["llm_deflected"] = result["deflected"]
                    entry["llm_reason"]    = result["reason"]
                    print(f"  Turn {i+1}: "
                          f"{'deflected' if result['deflected'] else 'DIRECT'} "
                          f"— {result['reason']}")

                print_summary(log, label="deflected")
                print_summary(log, label="llm_deflected")

                if args.turns:
                    print_turn_table(log, label="llm_deflected")
            else:
                print_summary(log, label="deflected")
                if args.turns:
                    print_turn_table(log, label="deflected")


if __name__ == "__main__":
    main()
