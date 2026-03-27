#!/usr/bin/env python3
"""
ingest.py

Convert a raw conversation transcript to analysis_input format, optionally
running post-hoc evaluation immediately.

Usage:
    python ingest.py <transcript.json> [options]

The raw transcript format (raw-transcript-v1):
    {
        "topic":         "Extensive form games in game theory",  // required
        "turns": [                                               // required
            { "role": "tutor",   "content": "..." },
            { "role": "student", "content": "..." }
        ],

        // Domain map source — choose one (topic string used if none provided):
        "wikipedia_url": "https://en.wikipedia.org/wiki/...",  // optional
        "domain_map":    {...} or "path/to/map.json",          // optional; highest priority

        // BKT initialization (default: "absent" — all KCs start at p=0.10):
        "bkt_preset":    "prereqs_mastered",  // root KCs mastered, others absent

        // Optional metadata (no effect on scores):
        "session_id":    "my-session-001",
        "source":        "gpt-4o-export",
        "date":          "2026-03-15"
    }

Two-stage workflow (recommended):
    # Stage 1 — generate analysis_input (includes domain map)
    python ingest.py transcript.json

    # Inspect the generated analysis_input (check KC names and graph quality)
    cat transcript_analysis_input.json | python3 -m json.tool | head -60

    # Stage 2 — score
    python score.py transcript_analysis_input.json -o result.json

One-shot workflow:
    python ingest.py transcript.json --score -o result.json

Disable NAC (faster, no Haiku calls for compliance):
    python ingest.py transcript.json --score --no-nac -o result.json

Skip domain map enrichment (faster, but knowledge_type defaults to "concept"):
    python ingest.py transcript.json --no-enrich --score
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_CACHE_DIR = str(Path.home() / ".socratic-domain-cache")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest a raw transcript and convert it to analysis_input format.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input",
        help="Path to raw transcript JSON file (raw-transcript-v1 format)",
    )
    parser.add_argument(
        "--output-input",
        metavar="FILE",
        help=(
            "Write generated analysis_input JSON to FILE "
            "(default: <stem>_analysis_input.json alongside input)"
        ),
    )
    parser.add_argument(
        "--score",
        action="store_true",
        help="Also run analyze_transcript() after generating analysis_input",
    )
    parser.add_argument(
        "--no-nac",
        action="store_true",
        help="Disable NAC when scoring (skips per-turn Haiku compliance calls)",
    )
    parser.add_argument(
        "--output-result", "-o",
        metavar="FILE",
        help="Write scoring result JSON to FILE (only with --score; default: stdout)",
    )
    parser.add_argument(
        "--cache-dir",
        metavar="DIR",
        default=DEFAULT_CACHE_DIR,
        help=f"Domain map cache directory (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--domain-map",
        metavar="FILE",
        help="Override domain map: load from FILE instead of generating one",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help=(
            "Skip enrichment pass when generating domain map (faster, but "
            "knowledge_type will default to 'concept' for all KCs)"
        ),
    )
    args = parser.parse_args()

    # --- Load raw transcript ---
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    try:
        raw = json.loads(input_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON in {input_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Validate ---
    from tutor_eval.ingestion.schema import validate_raw_transcript
    errors, warnings = validate_raw_transcript(raw)

    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # --domain-map flag overrides any source in the transcript
    if args.domain_map:
        raw = dict(raw)
        raw["domain_map"] = args.domain_map
        raw.pop("wikipedia_url", None)

    # --- Resolve domain map ---
    import anthropic
    from tutor_eval.ingestion.domain_resolver import DEFAULT_CACHE_DIR as _DEFAULT_CACHE, resolve_domain_map

    cache_dir = Path(args.cache_dir)
    client = anthropic.Anthropic()

    try:
        domain_map = resolve_domain_map(
            raw,
            client=client,
            cache_dir=cache_dir,
            skip_enrich=args.no_enrich,
        )
    except Exception as exc:
        print(f"Error resolving domain map: {exc}", file=sys.stderr)
        sys.exit(1)

    kc_count = len(domain_map.get("core_concepts", []))
    if kc_count == 0:
        print(
            "Warning: domain map has 0 KCs — evaluation metrics will be 0. "
            "Try a more specific topic or provide a domain map manually.",
            file=sys.stderr,
        )

    # --- Convert to analysis_input ---
    from tutor_eval.ingestion.converter import prepare_analysis_input
    analysis_input = prepare_analysis_input(raw, domain_map)

    # --- Write analysis_input ---
    if args.output_input:
        ai_path = Path(args.output_input)
    else:
        ai_path = input_path.with_name(input_path.stem + "_analysis_input.json")

    ai_path.write_text(json.dumps(analysis_input, indent=2))
    print(
        f"analysis_input written to {ai_path} "
        f"({kc_count} KCs, {len(analysis_input['lesson_turns'])} turns)",
        file=sys.stderr,
    )

    # --- Optionally score ---
    if not args.score:
        return

    from tutor_eval.evaluation.analyzer import analyze_transcript
    result = analyze_transcript(analysis_input, client=client, compute_nac=not args.no_nac)
    output = json.dumps(result.to_dict(), indent=2)

    if args.output_result:
        out_path = Path(args.output_result)
        out_path.write_text(output)
        print(f"Results written to {out_path}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
