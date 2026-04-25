"""
convolearn/score_batch.py

Full ConvoLearn evaluation pipeline CLI.

Stages:
  1  parse        — load dataset, sample prompts → sampled_dialogues.json
  2  domain_maps  — generate domain maps per prompt → domain_maps.json
  3  adapt        — convert dialogues to analysis_input (in-memory)
  4  score        — run analyze_transcript in parallel → scored_results.json
  5  summarise    — per-prompt aggregate stats → summary.json

Common workflows:

  # Inspect the selected sample before any API calls
  python -m convolearn.score_batch --parse-only --sample-size 3

  # Score a small slice to validate (re-uses existing sampled_dialogues.json)
  python -m convolearn.score_batch --from-sample --max-dialogues-per-prompt 3 --no-nac --no-lcq

  # Full production run
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
    parser.add_argument("--min-messages", type=int, default=10)
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
        help=(
            "sentence (default): compact 4–7 KC map, no enrichment pass — "
            "calibrated to ConvoLearn's brief Q&A sessions. "
            "article: full 12–20 KC enriched map."
        ),
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output-dir", default="convolearn/results/")
    parser.add_argument(
        "--parse-only", action="store_true",
        help="Run Stage 1 only: sample the dataset, print a summary, and exit. "
             "No API calls are made. Use this to inspect the selected prompts "
             "before committing to a full scoring run.",
    )
    parser.add_argument(
        "--from-sample", action="store_true",
        help="Skip Stage 1: load sampled_dialogues.json from --output-dir instead "
             "of re-downloading and re-sampling the dataset. Also skips Stage 2 "
             "if domain_maps.json already exists in --output-dir.",
    )
    parser.add_argument(
        "--max-dialogues-per-prompt", type=int, default=None, metavar="N",
        help="Cap the number of dialogues scored per prompt. Dialogues are taken "
             "in the order they appear in sampled_dialogues.json (index order). "
             "Useful for cheap validation runs before a full batch.",
    )
    parser.add_argument(
        "--append", action="store_true",
        help="Append new scores to an existing scored_results.json rather than "
             "overwriting it. Already-scored session IDs are skipped, so multiple "
             "small runs accumulate into one file. The summary is recomputed over "
             "all combined results after each run.",
    )
    parser.add_argument(
        "--sample-append", action="store_true",
        help="Append newly sampled prompts to an existing sampled_dialogues.json rather "
             "than overwriting it. Already-sampled prompt IDs are excluded from the new "
             "draw so each run adds fresh topics.",
    )
    args = parser.parse_args()

    # Map tabula_rasa alias
    bkt_preset = "absent" if args.initial_knowledge == "tabula_rasa" else args.initial_knowledge

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sampled_dialogues_path = output_dir / "sampled_dialogues.json"
    domain_maps_path = output_dir / "domain_maps.json"

    # ---- Stage 1: Parse & Sample (or load from disk) ----
    if args.from_sample:
        if not sampled_dialogues_path.exists():
            print(
                f"[error] --from-sample requires {sampled_dialogues_path} to exist. "
                "Run without --from-sample first.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"\n=== Stage 1: Loading existing sample from {sampled_dialogues_path} ===", flush=True)
        with open(sampled_dialogues_path) as f:
            sampled_prompts = json.load(f)
        total_dialogues = sum(len(p["dialogues"]) for p in sampled_prompts)
        print(
            f"[parse] Loaded {len(sampled_prompts)} prompts / {total_dialogues} dialogues",
            flush=True,
        )
    else:
        print("\n=== Stage 1: Parse & Sample ===", flush=True)
        existing_sample: list[dict] = []
        exclude_ids: set[str] = set()
        if args.sample_append and sampled_dialogues_path.exists():
            with open(sampled_dialogues_path) as f:
                existing_sample = json.load(f)
            exclude_ids = {p["prompt_id"] for p in existing_sample}
            print(f"[parse] --sample-append: {len(existing_sample)} existing prompts loaded", flush=True)
        new_prompts = load_and_sample(
            dataset_name=args.dataset,
            min_dialogues=args.min_dialogues,
            min_messages=args.min_messages,
            sample_size=args.sample_size,
            seed=args.seed,
            exclude_ids=exclude_ids,
        )
        sampled_prompts = existing_sample + new_prompts
        with open(sampled_dialogues_path, "w") as f:
            json.dump(sampled_prompts, f, indent=2)
        total_dialogues = sum(len(p["dialogues"]) for p in sampled_prompts)
        print(
            f"[parse] Wrote {len(sampled_prompts)} prompts / {total_dialogues} dialogues "
            f"→ {sampled_dialogues_path}",
            flush=True,
        )

    # ---- --parse-only: print summary and exit ----
    if args.parse_only:
        print("\n=== Selected prompts ===", flush=True)
        for i, entry in enumerate(sampled_prompts, 1):
            n = len(entry["dialogues"])
            q = entry["question_prompt"]
            q_display = q if len(q) <= 80 else q[:77] + "..."
            print(f"  {i}. [{entry['prompt_id']}]")
            print(f"     Topic : {entry['earthscience_topic']}")
            print(f"     Question: {q_display}")
            print(f"     Dialogues: {n}")
        total = sum(len(e["dialogues"]) for e in sampled_prompts)
        print(f"\nTotal: {len(sampled_prompts)} prompts, {total} dialogues")
        print(f"Sample written to: {sampled_dialogues_path}")
        print("\n(--parse-only: stopping before domain map generation and scoring)")
        return

    client = anthropic.Anthropic()

    # ---- Stage 2: Domain Maps (or load from disk) ----
    print(f"\n=== Stage 2: Domain Maps ===", flush=True)
    domain_maps: dict = {}
    if args.from_sample and domain_maps_path.exists():
        with open(domain_maps_path) as f:
            domain_maps = json.load(f)
        print(f"[domain-maps] Loaded {len(domain_maps)} cached maps", flush=True)

    missing = [p for p in sampled_prompts if p["prompt_id"] not in domain_maps]
    if missing:
        slim = (args.domain_source == "sentence")
        new_maps = generate_domain_maps(missing, client, slim=slim)
        domain_maps.update(new_maps)
        with open(domain_maps_path, "w") as f:
            json.dump(domain_maps, f, indent=2)
        print(f"[domain-maps] Generated {len(new_maps)} new maps → {domain_maps_path}", flush=True)
    else:
        print(f"[domain-maps] All {len(domain_maps)} maps already cached", flush=True)

    # ---- Stages 3+4: Adapt + Score (parallel) ----
    print(f"\n=== Stages 3+4: Adapt & Score ({args.workers} workers) ===", flush=True)

    # Build the candidate work list (all dialogues in the current sample)
    work_items: list[tuple] = []
    for entry in sampled_prompts:
        pid = entry["prompt_id"]
        qp = entry["question_prompt"]
        topic = entry["earthscience_topic"]
        dm = domain_maps.get(pid, {})
        for dialogue in entry["dialogues"]:
            work_items.append((pid, qp, topic, dialogue, dm))

    # Apply --max-dialogues-per-prompt cap to work items only (not to the saved file)
    if args.max_dialogues_per_prompt is not None:
        cap = args.max_dialogues_per_prompt
        seen: dict[str, int] = {}
        capped: list[tuple] = []
        for item in work_items:
            pid = item[0]
            seen[pid] = seen.get(pid, 0) + 1
            if seen[pid] <= cap:
                capped.append(item)
        print(
            f"[score] --max-dialogues-per-prompt={cap}: "
            f"{len(capped)} of {len(work_items)} dialogues selected",
            flush=True,
        )
        work_items = capped

    # Load existing results and skip already-scored sessions when --append
    existing_results: list[dict] = []
    scored_path = output_dir / "scored_results.json"
    if args.append and scored_path.exists():
        with open(scored_path) as f:
            existing_results = json.load(f)
        already_scored = {r["session_id"] for r in existing_results}
        before = len(work_items)
        work_items = [
            item for item in work_items
            if f"{item[0]}_{item[3]['dialogue_idx']}" not in already_scored
        ]
        skipped = before - len(work_items)
        print(
            f"[append] {len(existing_results)} existing records loaded; "
            f"{skipped} already-scored sessions skipped; "
            f"{len(work_items)} remaining to score",
            flush=True,
        )

    new_results: list[dict] = []
    completed = 0

    if work_items:
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
                new_results.append(rec)
                completed += 1
                status = "ERR" if rec.get("error") else "ok"
                print(
                    f"  [{completed}/{len(work_items)}] {rec['session_id']} [{status}]",
                    flush=True,
                )
    else:
        print("[score] Nothing to score — all sessions already present in scored_results.json", flush=True)

    # Combine with existing results (if --append), then sort deterministically
    all_results = existing_results + new_results
    all_results.sort(key=lambda r: (r["prompt_id"], r["session_id"]))

    # ---- Stage 5: Write Results ----
    print("\n=== Stage 5: Write Results ===", flush=True)

    with open(scored_path, "w") as f:
        json.dump(all_results, f, indent=2)
    if args.append and existing_results:
        print(
            f"[results] {len(existing_results)} existing + {len(new_results)} new = "
            f"{len(all_results)} total records → {scored_path}",
            flush=True,
        )
    else:
        print(f"[results] Wrote {len(all_results)} records → {scored_path}", flush=True)

    # Summary is built over all combined results using the full sample for prompt labels
    summary = build_summary(sampled_prompts, all_results)
    summary_path = output_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[results] Wrote summary ({len(summary)} prompts) → {summary_path}", flush=True)

    error_count = sum(1 for r in new_results if r.get("error"))
    if error_count:
        print(f"\n[WARNING] {error_count} dialogues failed — check error field in scored_results.json", flush=True)
    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()
