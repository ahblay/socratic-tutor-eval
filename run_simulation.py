#!/usr/bin/env python3
"""
run_simulation.py — CLI entrypoint for the tutor_eval assessment framework.

Usage:
    python run_simulation.py --profile tabula_rasa --turns 12
    python run_simulation.py --profile partial_knowledge --turns 12
    python run_simulation.py --profile misconception_heavy --turns 18

    # With explicit topic:
    python run_simulation.py --profile tabula_rasa --topic "fractions and proportional reasoning" --turns 12

NOTE: Set ANTHROPIC_API_KEY before running.
NOTE: Run from a plain terminal, not inside a Claude Code session.
"""

import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a Socratic tutor simulation using the SDK-based tutor_eval package.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="Student profile name (e.g. tabula_rasa, partial_knowledge, misconception_heavy)",
    )
    parser.add_argument(
        "--topic",
        default="fractions and proportional reasoning",
        help="Topic for the tutoring session (default: 'fractions and proportional reasoning')",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=12,
        help="Number of dialogue turns to simulate (default: 12)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSONL file path (default: runs/<timestamp>_sdk_<profile>.jsonl)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Recompute domain map even if a cached version exists",
    )
    args = parser.parse_args()

    import anthropic
    from tutor_eval.student.profiles import load_kg, get_profile
    from tutor_eval.tutors.socratic import SocraticTutor, load_or_compute_domain_map, compute_domain_map
    from tutor_eval.simulation import run_simulation

    base_dir = Path(__file__).parent
    kg      = load_kg(base_dir / "data" / "junyi_kg.json")
    profile = get_profile(base_dir / "students.yaml", args.profile)

    client = anthropic.Anthropic()

    cache_dir = base_dir / ".socratic-domain-cache"

    if args.no_cache:
        print(f"--no-cache: recomputing domain map for topic: {args.topic!r}")
        domain_map = compute_domain_map(args.topic, client)
        # Still save to cache so subsequent runs without --no-cache can use it
        cache_dir.mkdir(parents=True, exist_ok=True)
        import re
        slug = args.topic.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")[:80]
        cache_file = cache_dir / f"{slug}.json"
        import json
        with open(cache_file, "w") as f:
            json.dump(domain_map, f, indent=2)
    else:
        print(f"Computing/loading domain map for topic: {args.topic!r}")
        domain_map = load_or_compute_domain_map(args.topic, cache_dir, client)

    print(
        f"Domain map loaded: {domain_map.get('topic', '?')} "
        f"({len(domain_map.get('core_concepts', []))} concepts)\n"
    )

    tutor = SocraticTutor(topic=args.topic, domain_map=domain_map)

    # Output file
    if args.output:
        output_file = args.output
    else:
        runs_dir = base_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
        stamp       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_file = str(runs_dir / f"{stamp}_sdk_{args.profile}.jsonl")

    print(f"Profile : {args.profile}")
    print(f"Topic   : {args.topic}")
    print(f"Turns   : {args.turns}")
    print(f"Output  : {output_file}\n")

    run_simulation(
        tutor=tutor,
        profile=profile,
        kg=kg,
        topic=args.topic,
        turns=args.turns,
        output_file=output_file,
    )


if __name__ == "__main__":
    main()
