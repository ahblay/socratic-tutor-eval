#!/usr/bin/env python3
"""
simulate.py

Run a tutoring session between an external LLM tutor and a student agent
(or a human student), then save the transcript for post-hoc evaluation.

Usage
-----
    python simulate.py config.yaml [options]

    # Run session, save transcript
    python simulate.py gpt4o_novice.yaml

    # Run + ingest + score in one step
    python simulate.py gpt4o_novice.yaml --score -o result.json

    # Human student mode (ignores student.preset — reads from stdin)
    python simulate.py gpt4o_novice.yaml --human

    # Override turns limit
    python simulate.py config.yaml --max-turns 30

Config file format (YAML)
--------------------------
See docs/simulate_config.md or the example below.

    topic: "Extensive form games in game theory"
    domain_map: "a2-domain-map.json"     # optional path or inline object
    # wikipedia_url: "..."               # alternative domain map source
    source: "gpt-4o"                     # stored in transcript, no effect on scoring

    # --- Socratic tutor (built-in; uses ANTHROPIC_API_KEY) ---
    tutor:
      type: socratic                     # uses the built-in SocraticTutor
      model: claude-sonnet-4-6          # optional; any Claude model ID

    # --- External / generic API tutor ---
    tutor:
      type: generic_api                  # default when type is omitted
      model: gpt-4o
      base_url: null                     # null = OpenAI; override for other providers
      api_key_env: OPENAI_API_KEY        # name of the env var holding the key
      max_tokens: 2048
      temperature: 1.0
      include_domain_map: false          # inject {domain_map_json} into prompt?
      system_prompt: |
        You are a Socratic tutor helping a student learn {topic}.
        Never give direct answers. Ask questions that guide discovery.

    student:
      type: llm                          # llm | human
      preset: novice                     # novice | partial_knowledge | expert | misconception_heavy
      base_model: haiku                  # haiku | sonnet
      misconception_count: 0             # how many misconceptions to inject

    opening_message: null                # null → "Hi, I'm trying to understand {topic}."
    max_turns: 20
    min_turns: 8
    session_id: null                     # null → auto UUID
    output: null                         # null → {session_id}_transcript.json
    verbose: true

Notes
-----
- API keys are always read from environment variables (never put them in the config).
- The domain map is embedded inline in the transcript — ingest.py will not
  regenerate it, so re-scoring is free.
- [SESSION_COMPLETE] in any response ends the session early; ended_by is
  flagged in the transcript _metadata.
- Ctrl-C saves a partial transcript (ended_by="interrupted").
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# System prompt rendering
# ---------------------------------------------------------------------------

def render_system_prompt(template: str, topic: str, domain_map: dict | None = None) -> str:
    """
    Substitute {topic} and optionally {domain_map_json} in the system prompt.
    Uses literal str.replace() to avoid issues with curly braces in JSON examples.
    """
    result = template.replace("{topic}", topic)
    if "{domain_map_json}" in result:
        if domain_map is not None:
            result = result.replace("{domain_map_json}", json.dumps(domain_map, indent=2))
        else:
            result = result.replace("{domain_map_json}", "")
    return result


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _require(cfg: dict, *keys: str) -> None:
    for key in keys:
        if not cfg.get(key):
            print(f"Error: config missing required field: {key!r}", file=sys.stderr)
            sys.exit(1)


def _get(cfg: dict, *keys: str, default=None):
    """Nested dict access: _get(cfg, "tutor", "model")."""
    node = cfg
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key, default)
    return node


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a tutoring session with an external LLM tutor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("config", help="Path to YAML config file")
    parser.add_argument(
        "--output", "-O",
        metavar="FILE",
        help="Transcript output path (default: {session_id}_transcript.json)",
    )
    parser.add_argument(
        "--max-turns", type=int, metavar="N",
        help="Override max_turns from config",
    )
    parser.add_argument(
        "--min-turns", type=int, metavar="N",
        help="Override min_turns from config",
    )
    parser.add_argument(
        "--human", action="store_true",
        help="Force human student mode (reads from stdin)",
    )
    parser.add_argument(
        "--ingest", action="store_true",
        help="Generate analysis_input JSON after session",
    )
    parser.add_argument(
        "--score", action="store_true",
        help="Ingest and score after session (implies --ingest)",
    )
    parser.add_argument(
        "--no-nac", action="store_true",
        help="Disable NAC when scoring (only with --score)",
    )
    parser.add_argument(
        "--output-result", "-o",
        metavar="FILE",
        help="Write scoring result to FILE (only with --score; default: stdout)",
    )
    parser.add_argument(
        "--no-enrich", action="store_true",
        help="Skip domain map enrichment pass (faster; knowledge_type defaults to concept)",
    )
    parser.add_argument(
        "--cache-dir", metavar="DIR",
        default=str(Path.home() / ".socratic-domain-cache"),
        help="Domain map cache directory",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-turn output",
    )
    args = parser.parse_args()

    if args.score:
        args.ingest = True

    # --- Load config ---
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    _require(cfg, "topic")
    _require(cfg, "tutor")
    tutor_type = cfg.get("tutor", {}).get("type", "generic_api")
    if tutor_type != "socratic":
        _require(cfg.get("tutor", {}), "model", "system_prompt")

    topic = cfg["topic"]
    source = cfg.get("source")
    session_id = cfg.get("session_id")
    max_turns = args.max_turns or cfg.get("max_turns", 20)
    min_turns = args.min_turns or cfg.get("min_turns", 8)
    verbose = not args.quiet and cfg.get("verbose", True)
    opening_message = cfg.get("opening_message") or None

    # --- Resolve domain map ---
    import anthropic
    from tutor_eval.ingestion.domain_resolver import DEFAULT_CACHE_DIR, resolve_domain_map

    # Build a minimal "raw" dict for resolve_domain_map
    dm_raw: dict = {"topic": topic}
    if cfg.get("domain_map"):
        dm_raw["domain_map"] = cfg["domain_map"]
    if cfg.get("wikipedia_url"):
        dm_raw["wikipedia_url"] = cfg["wikipedia_url"]

    anthr_client = anthropic.Anthropic()
    try:
        domain_map = resolve_domain_map(
            dm_raw,
            client=anthr_client,
            cache_dir=Path(args.cache_dir),
            skip_enrich=args.no_enrich,
        )
    except Exception as exc:
        print(f"Error resolving domain map: {exc}", file=sys.stderr)
        sys.exit(1)

    kc_count = len(domain_map.get("core_concepts", []))
    if kc_count == 0:
        print(
            "Warning: domain map has 0 KCs. Evaluation metrics will all be 0.",
            file=sys.stderr,
        )

    # --- Build student profile ---
    student_cfg = cfg.get("student", {})
    student_type = "human" if args.human else student_cfg.get("type", "llm")

    profile = None
    kg = None
    student_agent = None

    if student_type == "llm":
        from tutor_eval.student.agent import StudentAgent
        from tutor_eval.student.domain_profile import generate_profile

        preset = student_cfg.get("preset", "novice")
        base_model = student_cfg.get("base_model", "haiku")
        misconception_count = int(student_cfg.get("misconception_count", 0))

        try:
            profile, kg = generate_profile(
                domain_map,
                preset=preset,
                misconception_count=misconception_count,
                base_model=base_model,
            )
        except ValueError as exc:
            print(f"Error generating student profile: {exc}", file=sys.stderr)
            sys.exit(1)

        student_agent = StudentAgent(profile, kg)

        if verbose:
            print(
                f"Student profile [{preset}]: "
                f"{len(profile['mastered'])} mastered, "
                f"{len(profile['partial'])} partial, "
                f"{len(profile['absent'])} absent, "
                f"{len(profile['misconceptions'])} misconception(s)",
                file=sys.stderr,
            )

    # --- Build tutor ---
    tutor_cfg = cfg["tutor"]

    if tutor_type == "socratic":
        from tutor_eval.tutors.socratic import SocraticTutor

        tutor = SocraticTutor(
            topic=topic,
            domain_map=domain_map,
            model=tutor_cfg.get("model", "claude-sonnet-4-6"),
        )
        if verbose:
            print(
                f"Tutor: SocraticTutor (model={tutor_cfg.get('model', 'claude-sonnet-4-6')})",
                file=sys.stderr,
            )

    else:
        from tutor_eval.tutors.external import GenericAPITutor

        api_key_env = tutor_cfg.get("api_key_env", "OPENAI_API_KEY")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            print(
                f"Error: environment variable {api_key_env!r} is not set.\n"
                f"Export it in your shell before running simulate.py.",
                file=sys.stderr,
            )
            sys.exit(1)

        include_domain_map = tutor_cfg.get("include_domain_map", False)
        rendered_prompt = render_system_prompt(
            tutor_cfg["system_prompt"],
            topic=topic,
            domain_map=domain_map if include_domain_map else None,
        )

        tutor = GenericAPITutor(
            model=tutor_cfg["model"],
            system_prompt=rendered_prompt,
            base_url=tutor_cfg.get("base_url") or None,
            api_key=api_key,
            max_tokens=int(tutor_cfg.get("max_tokens", 2048)),
            temperature=float(tutor_cfg.get("temperature", 1.0)),
        )
        if verbose:
            print(
                f"Tutor: GenericAPITutor (model={tutor_cfg['model']})",
                file=sys.stderr,
            )

    # --- Determine output path ---
    if args.output:
        output_path = Path(args.output)
    elif cfg.get("output"):
        output_path = Path(cfg["output"])
    else:
        sid = session_id or cfg.get("session_id") or "session"
        scratch = Path("scratch")
        scratch.mkdir(exist_ok=True)
        output_path = scratch / f"{sid}_transcript.json"

    # --- Run session ---
    from tutor_eval.student.domain_profile import bkt_states_from_profile
    from tutor_eval.session import run_session

    bkt_initial = bkt_states_from_profile(profile, kg) if (profile and kg) else {}

    transcript = run_session(
        tutor=tutor,
        domain_map=domain_map,
        topic=topic,
        student_type=student_type,
        student_agent=student_agent,
        profile=profile,
        kg=kg,
        bkt_initial_states=bkt_initial,
        opening_message=opening_message,
        max_turns=max_turns,
        min_turns=min_turns,
        session_id=session_id,
        source=source or tutor_cfg.get("model"),
        verbose=verbose,
        output_file=str(output_path),
    )

    # --- Ingest ---
    if not args.ingest:
        return

    from tutor_eval.ingestion.converter import prepare_analysis_input
    from tutor_eval.ingestion.domain_resolver import normalize_domain_map
    from tutor_eval.ingestion.schema import validate_raw_transcript

    errors, warnings = validate_raw_transcript(transcript)
    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    analysis_input = prepare_analysis_input(transcript, domain_map)
    ai_path = output_path.with_name(output_path.stem + "_analysis_input.json")
    ai_path.write_text(json.dumps(analysis_input, indent=2))
    print(f"analysis_input written to {ai_path}", file=sys.stderr)

    # --- Score ---
    if not args.score:
        return

    from tutor_eval.evaluation.analyzer import analyze_transcript

    result = analyze_transcript(
        analysis_input,
        client=anthr_client,
        compute_nac=not args.no_nac,
    )
    output_json = json.dumps(result.to_dict(), indent=2)

    if args.output_result:
        Path(args.output_result).write_text(output_json)
        print(f"Results written to {args.output_result}", file=sys.stderr)
    else:
        print(output_json)


if __name__ == "__main__":
    main()
