"""
convolearn/score_batch.py

Full ConvoLearn evaluation pipeline CLI.

Stages:
  1  parse        — load dataset, sample prompts → sampled_dialogues.json
  2  domain_maps  — generate domain maps per prompt → domain_maps.json
  3  adapt        — convert dialogues to analysis_input (in-memory)
  4  score        — run analyze_transcript in parallel → scored_results.json
  5  summarise    — per-prompt aggregate stats → summary.json

Run:
  python -m convolearn.score_batch --dataset masharma/convolearn --no-nac --no-lcq
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic

from convolearn.adapter import adapt_dialogue
from convolearn.domain_maps import generate_domain_maps
from convolearn.parse import load_and_sample
from tutor_eval.evaluation.analyzer import analyze_transcript


# ---------------------------------------------------------------------------
# Scoring worker
# ---------------------------------------------------------------------------

def _score_one(
    prompt_id: str,
    question_prompt: str,
    earthscience_topic: str,
    dialogue: dict,
    domain_map: dict,
    client: anthropic.Anthropic,
    bkt_preset: str,
    compute_nac: bool,
    report_nac: bool,
    report_lcq: bool,
) -> dict:
    """Score a single dialogue; returns a flat record for scored_results.json."""
    session_id = f"{prompt_id}_{dialogue['dialogue_idx']}"
    try:
        analysis_input = adapt_dialogue(
            prompt_id=prompt_id,
            question_prompt=question_prompt,
            dialogue=dialogue,
            domain_map=domain_map,
            bkt_preset=bkt_preset,
        )
        result = analyze_transcript(analysis_input, client, compute_nac=compute_nac)
        return {
            "session_id": session_id,
            "prompt_id": prompt_id,
            "earthscience_topic": earthscience_topic,
            "nac": result.nac if report_nac else None,
            "kft": result.kft,
            "pr": result.pr,
            "lcq": result.lcq if report_lcq else None,
            "mrq": result.mrq,
            "composite": result.composite,
            "is_valid": result.is_valid,
            "total_tutor_turns": result.total_tutor_turns,
            "effectiveness_consensus": dialogue.get("effectiveness_consensus"),
            "completeness_consensus": dialogue.get("completeness_consensus"),
            "num_exchanges": dialogue.get("num_exchanges"),
            "error": None,
        }
    except Exception as exc:
        print(f"  [score] ERROR {session_id}: {exc}", file=sys.stderr)
        return {
            "session_id": session_id,
            "prompt_id": prompt_id,
            "earthscience_topic": earthscience_topic,
            "nac": None, "kft": None, "pr": None, "lcq": None,
            "mrq": None, "composite": None, "is_valid": False,
            "total_tutor_turns": 0,
            "effectiveness_consensus": dialogue.get("effectiveness_consensus"),
            "completeness_consensus": dialogue.get("completeness_consensus"),
            "num_exchanges": dialogue.get("num_exchanges"),
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Summary aggregation
# ---------------------------------------------------------------------------

def _mean_or_none(values: list) -> float | None:
    non_null = [v for v in values if v is not None]
    if not non_null:
        return None
    return round(sum(non_null) / len(non_null), 4)


def build_summary(
    sampled_prompts: list[dict],
    scored_results: list[dict],
) -> list[dict]:
    """Compute per-prompt aggregate statistics."""
    # Index results by prompt_id
    by_prompt: dict[str, list[dict]] = {}
    for rec in scored_results:
        by_prompt.setdefault(rec["prompt_id"], []).append(rec)

    summary: list[dict] = []
    for entry in sampled_prompts:
        pid = entry["prompt_id"]
        records = by_prompt.get(pid, [])
        summary.append({
            "prompt_id": pid,
            "question_prompt": entry["question_prompt"],
            "n_dialogues": len(records),
            "mean_nac": _mean_or_none([r["nac"] for r in records]),
            "mean_kft": _mean_or_none([r["kft"] for r in records]),
            "mean_pr": _mean_or_none([r["pr"] for r in records]),
            "mean_lcq": _mean_or_none([r["lcq"] for r in records]),
            "mean_mrq": _mean_or_none([r["mrq"] for r in records]),
            "mean_composite": _mean_or_none([r["composite"] for r in records]),
            "mean_effectiveness_consensus": _mean_or_none(
                [r["effectiveness_consensus"] for r in records]
            ),
            "mean_completeness_consensus": _mean_or_none(
                [r["completeness_consensus"] for r in records]
            ),
        })
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the ConvoLearn evaluation pipeline."
    )
    parser.add_argument("--dataset", default="masharma/convolearn")
    parser.add_argument("--sample-size", type=int, default=7)
    parser.add_argument("--min-dialogues", type=int, default=20)
    parser.add_argument("--min-exchanges", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--nac", dest="nac", action="store_true", default=True,
        help="Enable NAC scoring (default)"
    )
    parser.add_argument(
        "--no-nac", dest="nac", action="store_false",
        help="Disable NAC scoring; emit nac=null in output"
    )
    parser.add_argument(
        "--lcq", dest="lcq", action="store_true", default=True,
        help="Enable LCQ scoring (default)"
    )
    parser.add_argument(
        "--no-lcq", dest="lcq", action="store_false",
        help="Disable LCQ scoring; emit lcq=null in output"
    )
    parser.add_argument(
        "--initial-knowledge", default="tabula_rasa",
        choices=["tabula_rasa", "absent", "prereqs_mastered", "all_partial"],
        help="BKT prior preset (tabula_rasa=absent)"
    )
    parser.add_argument(
        "--domain-source", default="sentence",
        choices=["sentence", "article"],
        help="Domain map source: sentence (default) or LLM-expanded article"
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output-dir", default="convolearn/results/")
    args = parser.parse_args()

    # Map tabula_rasa alias
    bkt_preset = "absent" if args.initial_knowledge == "tabula_rasa" else args.initial_knowledge

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    client = anthropic.Anthropic()

    # ---- Stage 1: Parse & Sample ----
    print("\n=== Stage 1: Parse & Sample ===", flush=True)
    sampled_dialogues_path = output_dir / "sampled_dialogues.json"
    sampled_prompts = load_and_sample(
        dataset_name=args.dataset,
        min_dialogues=args.min_dialogues,
        min_exchanges=args.min_exchanges,
        sample_size=args.sample_size,
        seed=args.seed,
    )
    with open(sampled_dialogues_path, "w") as f:
        json.dump(sampled_prompts, f, indent=2)
    total_dialogues = sum(len(p["dialogues"]) for p in sampled_prompts)
    print(
        f"[parse] Wrote {len(sampled_prompts)} prompts / {total_dialogues} dialogues "
        f"→ {sampled_dialogues_path}",
        flush=True,
    )

    # ---- Stage 2: Domain Maps ----
    print("\n=== Stage 2: Domain Maps ===", flush=True)
    domain_maps_path = output_dir / "domain_maps.json"
    domain_maps = generate_domain_maps(sampled_prompts, client)
    with open(domain_maps_path, "w") as f:
        json.dump(domain_maps, f, indent=2)
    print(f"[domain-maps] Wrote {len(domain_maps)} maps → {domain_maps_path}", flush=True)

    # ---- Stages 3+4: Adapt + Score (parallel) ----
    print(f"\n=== Stages 3+4: Adapt & Score ({args.workers} workers) ===", flush=True)

    # Build work items
    work_items: list[tuple] = []
    for entry in sampled_prompts:
        pid = entry["prompt_id"]
        qp = entry["question_prompt"]
        topic = entry["earthscience_topic"]
        dm = domain_maps.get(pid, {})
        for dialogue in entry["dialogues"]:
            work_items.append((pid, qp, topic, dialogue, dm))

    scored_results: list[dict] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                _score_one,
                pid, qp, topic, dialogue, dm,
                client, bkt_preset,
                args.nac, args.nac, args.lcq,
            ): (pid, dialogue["dialogue_idx"])
            for pid, qp, topic, dialogue, dm in work_items
        }
        for future in as_completed(futures):
            rec = future.result()
            scored_results.append(rec)
            completed += 1
            status = "ERR" if rec.get("error") else "ok"
            print(
                f"  [{completed}/{len(work_items)}] {rec['session_id']} [{status}]",
                flush=True,
            )

    # Sort deterministically: prompt_id, then dialogue_idx
    scored_results.sort(key=lambda r: (r["prompt_id"], r["session_id"]))

    # ---- Stage 5: Write Results ----
    print("\n=== Stage 5: Write Results ===", flush=True)

    scored_path = output_dir / "scored_results.json"
    with open(scored_path, "w") as f:
        json.dump(scored_results, f, indent=2)
    print(f"[results] Wrote {len(scored_results)} records → {scored_path}", flush=True)

    summary = build_summary(sampled_prompts, scored_results)
    summary_path = output_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[results] Wrote summary ({len(summary)} prompts) → {summary_path}", flush=True)

    error_count = sum(1 for r in scored_results if r.get("error"))
    if error_count:
        print(f"\n[WARNING] {error_count} dialogues failed — check error field in scored_results.json", flush=True)
    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()
