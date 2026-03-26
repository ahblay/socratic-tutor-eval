#!/usr/bin/env python3
"""
score.py

CLI for offline post-hoc transcript analysis.

Usage:
    python score.py <analysis_input.json> [--no-nac] [--output <out.json>]

Reads an analysis-input JSON file (same format returned by
GET /api/admin/sessions/{session_id}/analysis-input) and prints the
EvaluationResult to stdout as JSON, or writes it to a file.

Example:
    # Fetch the input:
    curl -s http://localhost:8000/api/admin/sessions/<id>/analysis-input \\
        -H "Authorization: Bearer $TOKEN" > session_input.json

    # Score it:
    python score.py session_input.json

    # Score without NAC (faster, no Haiku calls for compliance):
    python score.py session_input.json --no-nac --output result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score a session transcript using post-hoc BKT analysis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "input",
        help="Path to analysis-input JSON file",
    )
    parser.add_argument(
        "--no-nac",
        action="store_true",
        help="Disable NAC (sets nac=1.0, skips per-turn Haiku calls for compliance)",
    )
    parser.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Write JSON result to FILE instead of stdout",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    try:
        analysis_input = json.loads(input_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON in {input_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    from tutor_eval.evaluation.analyzer import analyze_transcript

    result = analyze_transcript(analysis_input, compute_nac=not args.no_nac)
    output = json.dumps(result.to_dict(), indent=2)

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(output)
        print(f"Results written to {out_path}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
