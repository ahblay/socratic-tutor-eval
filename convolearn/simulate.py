"""
convolearn/simulate.py

Multi-condition tutor comparison simulation CLI.

Stages:
  1  Load domain maps + topic list from sampled_dialogues.json
  2  Build work list: (topic, condition, rep) tuples; skip if --append
  3  Run dialogues in parallel (ThreadPoolExecutor)
  4  Convert raw turns → analysis_input via prepare_analysis_input()
  5  Score with analyze_transcript()
  6  Compute pilot_composite and nac_adjusted_composite
  7  Write sim_results.json, sim_summary.json, sim_transcripts.json

Usage:
  # Smoke test
  python -m convolearn.simulate \\
    --conditions socratic,bare-map \\
    --turns 10 --reps 1 \\
    --prompt-id i-m-looking-at-a-topographic-map-with-a-contour-interval-of-5-meters-how-can-i-d \\
    --output-dir convolearn/results/sim/

  # Pilot run
  python -m convolearn.simulate \\
    --conditions socratic,instructed-map,bare-map \\
    --turns 10 --reps 8 \\
    --prompt-id i-m-looking-at-a-topographic-map-with-a-contour-interval-of-5-meters-how-can-i-d \\
    --output-dir convolearn/results/sim/
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic

from convolearn.adapter import _flatten_domain_map
from convolearn.sim_conditions import CONDITIONS, TOPIC_SLUGS
from tutor_eval.evaluation.analyzer import analyze_transcript
from tutor_eval.ingestion.converter import prepare_analysis_input
from tutor_eval.student.convolearn_agent import ConvoLearnStudentAgent
from tutor_eval.tutors.generic import GenericTutor
from tutor_eval.tutors.socratic import SocraticTutor


# ---------------------------------------------------------------------------
# Tutor factory
# ---------------------------------------------------------------------------

def _get_model(condition: str) -> str:
    return "claude-haiku-4-5-20251001"


def _model_short(model: str) -> str:
    if "sonnet" in model:
        return "sonnet"
    if "haiku" in model:
        return "haiku"
    return model.split("-")[0]


def _build_tutor(condition: str, topic: str, domain_map: dict, api_base: str | None):
    prompt_level, _ = CONDITIONS[condition]
    if prompt_level == "socratic":
        return SocraticTutor(topic=topic, domain_map=domain_map, model="claude-haiku-4-5-20251001")
    return GenericTutor(
        topic=topic,
        domain_map=domain_map,
        prompt_level=prompt_level,
        api_base=api_base,
    )


# ---------------------------------------------------------------------------
# Dialogue loop
# ---------------------------------------------------------------------------

def _run_dialogue(
    question_prompt: str,
    domain_map: dict,
    condition: str,
    n_turns: int,
    api_base: str | None,
) -> list[dict]:
    """
    Run one student–tutor dialogue and return raw_turns.
    raw_turns uses "student"/"tutor" roles (not "user"/"tutor").
    n_turns is the number of TUTOR turns.
    """
    student = ConvoLearnStudentAgent(question_prompt=question_prompt)
    tutor = _build_tutor(condition, question_prompt, domain_map, api_base)

    # Shared history for tutor: {"role": "student"|"tutor", "text": str}
    history: list[dict] = []
    raw_turns: list[dict] = []

    # Student opens
    sr = student.generate_message(last_tutor=None, history=[])
    student_msg = sr["message"]
    history.append({"role": "student", "text": student_msg})
    raw_turns.append({"role": "student", "content": student_msg})

    for i in range(n_turns):
        # Tutor responds
        tutor_msg = tutor.respond(student_msg, history)
        history.append({"role": "tutor", "text": tutor_msg})
        raw_turns.append({"role": "tutor", "content": tutor_msg})

        # Student responds (except after the final tutor turn)
        if i < n_turns - 1:
            sr = student.generate_message(last_tutor=tutor_msg, history=history)
            student_msg = sr["message"]
            history.append({"role": "student", "text": student_msg})
            raw_turns.append({"role": "student", "content": student_msg})

    actual_tutor_turns = sum(1 for t in raw_turns if t["role"] == "tutor")
    if actual_tutor_turns != n_turns:
        print(
            f"  [warn] {condition}: expected {n_turns} tutor turns, got {actual_tutor_turns}",
            file=sys.stderr,
        )

    return raw_turns


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_session(
    session_id: str,
    prompt_id: str,
    question_prompt: str,
    domain_map: dict,
    raw_turns: list[dict],
    condition: str,
    model: str,
    rep: int,
    client: anthropic.Anthropic,
) -> tuple[dict, dict]:
    """Returns (flat_record, full_result_dict). full_result_dict includes turn_results."""
    flat_map = _flatten_domain_map(domain_map)
    raw = {
        "session_id": session_id,
        "topic": question_prompt,
        "turns": raw_turns,
        "bkt_preset": "absent",
    }
    analysis_input = prepare_analysis_input(raw, flat_map)
    result = analyze_transcript(analysis_input, client, compute_nac=True)

    mrq_adj = result.mrq_adjustment
    inner = 0.667 * result.kft + 0.333 * result.pr + mrq_adj
    pilot_composite = round(result.nac * inner, 4)
    nac_adjusted_composite = round(inner, 4)

    flat_record = {
        "session_id": session_id,
        "prompt_id": prompt_id,
        "condition": condition,
        "model": model,
        "rep": rep,
        "nac": result.nac,
        "kft": result.kft,
        "pr": result.pr,
        "lcq": result.lcq,
        "mrq": result.mrq,
        "mrq_adjustment": mrq_adj,
        "pilot_composite": pilot_composite,
        "nac_adjusted_composite": nac_adjusted_composite,
        "analyzer_composite": round(result.composite, 4),
        "is_valid": result.is_valid,
        "total_tutor_turns": result.total_tutor_turns,
        "error": None,
    }
    return flat_record, result.to_dict()


# ---------------------------------------------------------------------------
# Worker: dialogue + score
# ---------------------------------------------------------------------------

def _run_one(
    session_id: str,
    prompt_id: str,
    question_prompt: str,
    domain_map: dict,
    condition: str,
    model: str,
    rep: int,
    n_turns: int,
    api_base: str | None,
    client: anthropic.Anthropic,
) -> tuple[dict, dict]:
    """Run dialogue + score. Returns (result_record, transcript_record)."""
    try:
        raw_turns = _run_dialogue(question_prompt, domain_map, condition, n_turns, api_base)
        result_record, full_result = _score_session(
            session_id, prompt_id, question_prompt, domain_map,
            raw_turns, condition, model, rep, client,
        )
        transcript_record = {
            "session_id": session_id,
            "topic": question_prompt,
            "domain_map": domain_map,
            "turns": raw_turns,
            "full_result": full_result,
        }
        return result_record, transcript_record
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        result_record = {
            "session_id": session_id,
            "prompt_id": prompt_id,
            "condition": condition,
            "model": model,
            "rep": rep,
            "nac": None, "kft": None, "pr": None, "lcq": None,
            "mrq": None, "mrq_adjustment": None,
            "pilot_composite": None, "nac_adjusted_composite": None,
            "analyzer_composite": None,
            "is_valid": False,
            "total_tutor_turns": None,
            "error": str(e),
        }
        transcript_record = {
            "session_id": session_id,
            "topic": question_prompt,
            "domain_map": domain_map,
            "turns": [],
        }
        return result_record, transcript_record


# ---------------------------------------------------------------------------
# Summary aggregation
# ---------------------------------------------------------------------------

def _summarize(results: list[dict]) -> list[dict]:
    by_condition: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_condition[r["condition"]].append(r)

    summaries = []
    for condition, recs in sorted(by_condition.items()):
        model = recs[0]["model"] if recs else ""
        valid = [r for r in recs if r.get("is_valid") and r.get("error") is None]

        def _mean(key: str) -> float | None:
            vals = [r[key] for r in valid if r.get(key) is not None]
            return round(statistics.mean(vals), 4) if vals else None

        def _std(key: str) -> float | None:
            vals = [r[key] for r in valid if r.get(key) is not None]
            return round(statistics.stdev(vals), 4) if len(vals) >= 2 else None

        summaries.append({
            "condition": condition,
            "model": model,
            "n_sessions": len(valid),
            "mean_nac": _mean("nac"),
            "mean_kft": _mean("kft"),
            "mean_pr": _mean("pr"),
            "mean_lcq": _mean("lcq"),
            "mean_mrq": _mean("mrq"),
            "mean_pilot_composite": _mean("pilot_composite"),
            "std_pilot_composite": _std("pilot_composite"),
            "mean_nac_adjusted_composite": _mean("nac_adjusted_composite"),
            "std_nac_adjusted_composite": _std("nac_adjusted_composite"),
            "mean_analyzer_composite": _mean("analyzer_composite"),
        })

    return summaries


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-condition tutor simulation")
    parser.add_argument(
        "--conditions", default="socratic,instructed-map,bare-map",
        help="Comma-separated condition IDs (socratic, instructed-map, bare-map)",
    )
    parser.add_argument("--turns", type=int, default=10, help="Tutor turns per session")
    parser.add_argument("--reps", type=int, default=8, help="Repetitions per condition")
    parser.add_argument(
        "--prompt-id",
        default="i-m-looking-at-a-topographic-map-with-a-contour-interval-of-5-meters-how-can-i-d",
        help="Prompt ID from sampled_dialogues.json",
    )
    parser.add_argument("--output-dir", default="convolearn/results/sim/")
    parser.add_argument(
        "--input-dir", default="convolearn/results/",
        help="Directory containing sampled_dialogues.json and domain_maps.json",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--api-base", default=None, help="Override API base URL for generic tutors")
    parser.add_argument(
        "--append", action="store_true",
        help="Append to existing outputs; skip already-scored session IDs",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    input_dir = Path(args.input_dir)

    conditions = [c.strip() for c in args.conditions.split(",")]
    for cond in conditions:
        if cond not in CONDITIONS:
            print(f"Unknown condition: {cond!r}. Valid: {list(CONDITIONS.keys())}", file=sys.stderr)
            sys.exit(1)

    # Load input data
    with open(input_dir / "sampled_dialogues.json") as f:
        sampled = json.load(f)
    with open(input_dir / "domain_maps.json") as f:
        domain_maps = json.load(f)

    prompt_entry = next((e for e in sampled if e["prompt_id"] == args.prompt_id), None)
    if prompt_entry is None:
        print(f"Prompt ID not found: {args.prompt_id}", file=sys.stderr)
        sys.exit(1)

    question_prompt = prompt_entry["question_prompt"]
    domain_map = domain_maps[args.prompt_id]
    topic_slug = TOPIC_SLUGS.get(args.prompt_id, args.prompt_id[:20])

    # Load existing outputs for --append deduplication
    existing_results: list[dict] = []
    existing_transcripts: list[dict] = []
    existing_ids: set[str] = set()

    if args.append:
        rp = output_dir / "sim_results.json"
        tp = output_dir / "sim_transcripts.json"
        if rp.exists():
            with open(rp) as f:
                existing_results = json.load(f)
            existing_ids = {r["session_id"] for r in existing_results}
        if tp.exists():
            with open(tp) as f:
                existing_transcripts = json.load(f)

    # Build work list
    work = []
    for condition in conditions:
        model = _get_model(condition)
        m_short = _model_short(model)
        for rep in range(args.reps):
            session_id = f"{topic_slug}_{condition}_{m_short}_{rep}"
            if session_id in existing_ids:
                print(f"  [skip] {session_id} (already scored)")
                continue
            work.append((session_id, args.prompt_id, question_prompt, domain_map, condition, model, rep))

    print(f"=== Simulation: {len(work)} sessions to run ({args.workers} workers) ===")
    if not work:
        print("Nothing to do.")
        return

    client = anthropic.Anthropic()
    new_results: list[dict] = []
    new_transcripts: list[dict] = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _run_one,
                sid, pid, qp, dm, cond, model, rep,
                args.turns, args.api_base, client,
            ): sid
            for sid, pid, qp, dm, cond, model, rep in work
        }
        for future in as_completed(futures):
            sid = futures[future]
            try:
                result_record, transcript_record = future.result()
                new_results.append(result_record)
                new_transcripts.append(transcript_record)
                err = result_record.get("error")
                status = f"ERR: {err[:60]}" if err else "OK"
                print(f"  [done] {sid} — {status}")
            except Exception as e:
                print(f"  [fatal] {sid}: {e}", file=sys.stderr)

    all_results = existing_results + new_results
    all_transcripts = existing_transcripts + new_transcripts

    with open(output_dir / "sim_results.json", "w") as f:
        json.dump(all_results, f, indent=2)
    with open(output_dir / "sim_transcripts.json", "w") as f:
        json.dump(all_transcripts, f, indent=2)

    summary = _summarize(all_results)
    with open(output_dir / "sim_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n=== Written to {output_dir} ===")
    print(f"  sim_results.json    — {len(all_results)} sessions")
    print(f"  sim_transcripts.json — {len(all_transcripts)} transcripts")
    print(f"  sim_summary.json    — {len(summary)} conditions")


if __name__ == "__main__":
    main()
