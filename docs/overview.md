# Project Overview

## What This Is

A two-part system for studying Socratic tutoring effectiveness:

1. **Evaluation Framework** (`tutor_eval/`): Runs simulated student–tutor dialogues using BKT to measure how well a Socratic tutor guides learning. Used for offline experimentation with synthetic student profiles.

2. **Wikipedia Socratic Tutor** (`webapp/`): A web application where real humans learn Wikipedia articles through Socratic dialogue. Its **primary purpose is data collection** — generating real student–tutor conversation transcripts that are later fed through the evaluation framework.

## Core Idea

A Socratic tutor never gives direct answers. It asks questions that guide the student to discover knowledge themselves. This project measures how effectively that works by tracking student knowledge state (via BKT) over the course of a tutoring session.

For real sessions (webapp), all evaluation is **post-hoc**: the webapp collects data, and `analyze_transcript()` runs the evaluation after the fact.

## Research Context

This is a research project at the University of Alberta (CMPUT658). The evaluation metrics and framework are described in `Socratic_Tutor.pdf` in the project root.

## Repository Layout

```
├── tutor_eval/          # Evaluation library (simulation + post-hoc analysis)
│   ├── tutors/          # Tutor implementations (AbstractTutor, SocraticTutor)
│   ├── student/         # StudentAgent + BKT-initialized profiles
│   ├── evaluation/      # BKTEvaluator, metrics
│   └── simulation.py    # run_simulation() loop
├── webapp/              # FastAPI web server
│   ├── api/             # Route handlers (auth, articles, sessions, assessment, export)
│   ├── db/              # SQLAlchemy models + async engine
│   ├── services/        # Wikipedia fetcher, domain map cache
│   ├── app.py           # FastAPI factory + lifespan
│   └── config.py        # Env var configuration
├── simulation/          # CLI entrypoint for offline simulations
│   ├── run.py           # main() with --profile, --topic, --turns flags
│   └── profiles/        # students.yaml (3 synthetic profiles)
├── tests/               # 76 tests (pytest-asyncio)
├── legacy/              # Old CLI files kept for reference
├── tools/               # visualize.html, summary.html (JSONL viewers)
├── data/                # junyi_kg.json KC graph (38 nodes)
└── docs/                # This directory
```

## Quick Start

```bash
# Setup
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Set API key
export ANTHROPIC_API_KEY=...

# Run a simulation
python simulation/run.py --profile tabula_rasa --turns 12

# Run tests
pytest

# Run webapp
uvicorn webapp.app:create_app --factory --reload
```

## Key Dependencies

- `anthropic` — Claude API (used by SocraticTutor and StudentAgent)
- `fastapi` + `uvicorn` — web server
- `sqlalchemy` + `aiosqlite` — async ORM
- `python-jose` — JWT auth
- `pyyaml` — student profile loading
