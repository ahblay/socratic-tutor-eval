# Project Overview

## What This Is

A two-part system for studying Socratic tutoring effectiveness:

1. **Evaluation Framework** (`tutor_eval/`): Runs simulated student–tutor dialogues using BKT to measure how well a Socratic tutor guides learning. Used for offline experimentation with synthetic student profiles.

2. **Wikipedia Socratic Tutor** (`webapp/`): A web application where real humans learn Wikipedia articles through Socratic dialogue. Its **dual purpose** is (a) data collection — generating real student–tutor conversation transcripts that are later fed through the evaluation framework — and (b) a long-term public service for guided learning.

## Core Idea

A Socratic tutor never gives direct answers. It asks questions that guide the student to discover knowledge themselves. This project measures how effectively that works by tracking student knowledge state (via BKT) over the course of a tutoring session.

For real sessions (webapp), all evaluation is **post-hoc**: the webapp collects data, and `analyze_transcript()` runs the evaluation after the fact.

## Research Context

This is a research project at the University of Alberta (CMPUT658). The evaluation metrics and framework are described in `Socratic_Tutor.pdf` in the project root.

## Long-Term Vision

The webapp is intended to eventually be a publicly available service for guided learning — not just a research data collection tool. The MVP focuses on learning a single Wikipedia article per session. Future work includes linking learning history to user accounts and spaced repetition.

Cost is a scaling concern: the current configuration (Sonnet for tutoring + Haiku for guardrail/assessment) is not viable for a large free user base. A hosted subscription or credits model will be assessed once the product is validated. See the API Key & Cost section below.

## Repository Layout

```
├── tutor_eval/          # Evaluation library (simulation + post-hoc analysis)
│   ├── tutors/          # Tutor implementations (AbstractTutor, SocraticTutor, GenericAPITutor)
│   ├── student/         # StudentAgent + domain-map-based profiles
│   ├── evaluation/      # BKTEvaluator, analyzer, metrics
│   └── ingestion/       # Raw transcript ingestion pipeline
├── webapp/              # FastAPI web server
│   ├── api/             # Route handlers (auth, articles, sessions, assessment, export)
│   ├── db/              # SQLAlchemy models + async engine
│   ├── services/        # Wikipedia fetcher, domain map cache
│   └── static/          # Vanilla JS frontend
├── configs/             # YAML session configs for simulate.py (named topic_preset.yaml)
├── tests/               # pytest test suite
├── data/                # junyi_kg.json KC graph (38 nodes)
├── scratch/             # gitignored — local artifacts (transcripts, results, domain maps)
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

# Run a simulation (transcript saved to scratch/)
python simulate.py configs/geodesy_novice.yaml

# Run tests
pytest

# Run webapp
uvicorn webapp.app:create_app --factory --reload
```

## API Key & Cost Model

**Current model: Server-hosted key + credits**

The server uses its own `ANTHROPIC_API_KEY` (set in the server environment). Users do not provide API keys.

Access is controlled via a **credit system**:
- Each tutoring turn costs 1 credit
- Superusers are exempt from credit checks
- Credits are granted by a superuser via `POST /api/admin/users/{user_id}/credits`
- The server returns HTTP 402 when credits are exhausted

Token usage (input/output tokens) is accumulated per session from API response metadata and stored in the `sessions` table for cost analysis.

**Future model**: When cost per session is well understood and the product is validated, a subscription or per-session purchase model will replace manual credit grants.

## Data Collection & Privacy

Users are informed at account creation that conversation transcripts are collected to evaluate tutor performance. A consent checkbox (not pre-checked) is required before registration completes.

Applicable privacy frameworks:
- **PIPEDA** (Canada, primary): satisfied by explicit consent at registration
- **GDPR** (EU users): basic consent satisfied; full compliance (right to deletion, DPO) is future work at scale

No personally identifiable information is shared with third parties. The Anthropic API receives conversation text but not user identity.

## Key Dependencies

- `anthropic` — Claude API (used by SocraticTutor and StudentAgent)
- `fastapi` + `uvicorn` — web server
- `sqlalchemy` + `aiosqlite` — async ORM
- `python-jose` — JWT auth
- `pyyaml` — student profile loading
- `@viz-js/viz` (CDN) — Graphviz/WASM for knowledge graph rendering
- `svg-pan-zoom` (CDN) — pan/zoom for the knowledge graph SVG
