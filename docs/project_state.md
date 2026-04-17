# Project State — Socratic Tutor Evaluation Framework

**Last updated:** 2026-04-17
**Purpose:** Authoritative, self-contained reference for the current state of the project.
Intended to give a new contributor (human or AI) enough context to begin productive work
immediately. Where it conflicts with older docs, this document takes precedence.

---

## What This Project Is

A research system for **measuring the quality of Socratic tutoring** using post-hoc
analysis of conversation transcripts. It has two distinct parts:

1. **Evaluation framework** (`tutor_eval/`) — a Python library and set of CLI tools
   that take a conversation transcript, reconstruct the student's knowledge state
   via Bayesian Knowledge Tracing (BKT), and compute a suite of pedagogical metrics.

2. **Webapp** (`webapp/`) — a FastAPI web application providing a live Socratic
   tutoring experience for Wikipedia articles. Its primary research purpose is data
   collection: real human sessions are stored in a database and can be scored with
   the evaluation framework.

The two parts share the domain map infrastructure and the `analyze_transcript()` function
but are otherwise independent. The evaluation framework can score any transcript; the
webapp generates transcripts and can trigger scoring via a REST API.

---

## Repository Structure

```
.
├── tutor_eval/                    # Evaluation library
│   ├── tutors/
│   │   ├── base.py                # AbstractTutor interface
│   │   ├── socratic.py            # SocraticTutor — main Claude-backed tutor
│   │   └── external.py            # GenericAPITutor — any OpenAI-compatible API
│   ├── student/
│   │   ├── agent.py               # StudentAgent — LLM-simulated student
│   │   ├── profiles.py            # load_kg(), load_profiles(), get_profile()
│   │   └── domain_profile.py      # generate_profile() — profiles from domain maps
│   ├── evaluation/
│   │   ├── bkt.py                 # BKT update rules, observation classifier (Haiku)
│   │   ├── analyzer.py            # analyze_transcript() — main evaluation entry point
│   │   └── metrics.py             # compute_nac/kft/pr/lcq/mrq/composite
│   ├── ingestion/
│   │   ├── schema.py              # validate_raw_transcript()
│   │   ├── domain_resolver.py     # resolve_domain_map() — generation, normalization, cache
│   │   └── converter.py           # prepare_analysis_input() — turns raw transcript to analyzer input
│   ├── simulation.py              # run_simulation() — original JSONL-output loop (legacy)
│   └── session.py                 # run_session() — new transcript-collection loop
│
├── webapp/                        # FastAPI web application
│   ├── main.py                    # App entry point, router registration
│   ├── api/
│   │   ├── auth.py                # JWT auth endpoints
│   │   ├── articles.py            # Article catalog, domain map generation
│   │   ├── sessions.py            # Session lifecycle, tutoring turns
│   │   ├── assessment.py          # Pre-session assessment
│   │   ├── admin.py               # Admin endpoints (analysis-input, user management)
│   │   └── export.py              # POST/GET analyze endpoints
│   ├── db/
│   │   └── models.py              # User, Article, Session, Turn, BKTStateRow
│   ├── services/
│   │   ├── domain_cache.py        # Domain map computation and caching
│   │   └── wikipedia.py           # Wikipedia article fetching
│   └── static/                    # Vanilla JS frontend (app.js, chat.js, graph.js, …)
│
├── score.py                       # CLI: analysis_input JSON → evaluation result
├── ingest.py                      # CLI: raw transcript → analysis_input (→ optional scoring)
├── simulate.py                    # CLI: YAML config → run session → transcript (→ optional scoring)
│
├── docs/
│   ├── project_state.md           # THIS FILE — authoritative current state
│   ├── transcript_analysis.md     # Raw transcript format, ingest.py, score.py reference
│   ├── evaluation_plan.md         # Metric design decisions (authoritative for metrics)
│   └── …                          # Older docs (may be partially outdated)
│
├── API.md                         # curl-based reference for all webapp admin workflows
├── KNOWN_ISSUES.md                # Documented bugs not yet addressed
└── requirements.txt               # Python dependencies
```

---

## Development Setup

```bash
git clone https://github.com/ahblay/socratic-tutor-eval.git
cd socratic-tutor-eval
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**API keys (add to `~/.zshrc`):**
```bash
export ANTHROPIC_API_KEY=sk-ant-...   # required for evaluation and SocraticTutor
export OPENAI_API_KEY=sk-...          # required for external tutor sessions (simulate.py)
```

**Run the webapp:**
```bash
source .venv/bin/activate
uvicorn webapp.main:app --reload
# → http://localhost:8000
```

**Domain map cache** lives at `~/.socratic-domain-cache/` (JSON files keyed by topic slug or
Wikipedia page ID). Cached maps are reused automatically — delete a file to force regeneration.

---

## Core Concepts

### Domain Map

A structured representation of the knowledge required for a lesson. The analyzer and all metrics
depend on it.

**Format** (webapp / normalized format):
```json
{
  "topic": "...",
  "core_concepts": [
    {
      "concept": "Information Sets",
      "prerequisite_for": ["Pure Strategies"],
      "knowledge_type": "concept"
    }
  ],
  "recommended_sequence": ["Information Sets", "Pure Strategies"],
  "common_misconceptions": [...]
}
```

`knowledge_type` is one of `"concept"` (student can reason to the answer), `"convention"`
(definition must be stated first, then applied), or `"narrative"` (structured content block
then reasoning). Missing `knowledge_type` defaults to `"concept"` and degrades LCQ accuracy.

KC IDs are derived by `_slugify(concept_name)` — lowercase, non-alphanumeric characters
replaced by hyphens, truncated to 64 characters. All components use this same function.

### Bayesian Knowledge Tracing (BKT)

BKT tracks the probability that a student has mastered each KC, updated after every student
turn. Parameters (hand-tuned):

| Parameter | Value | Meaning |
|---|---|---|
| P_L0 (absent) | 0.10 | Prior for unknown KC |
| P_L0 (partial) | 0.50 | Prior for partially known KC |
| P_L0 (mastered) | 0.90 | Prior for assumed-known KC |
| P_TRANSIT | 0.10 | Probability of learning per turn |
| P_GUESS | 0.25 | Probability of correct answer without knowledge |
| P_SLIP | 0.10 | Probability of incorrect answer despite knowledge |
| Mastery threshold | 0.70 | p_mastered ≥ 0.70 → KC considered mastered |

BKT is updated by an LLM classifier (Haiku) that reads each student turn and assigns one of:

| Class | BKT weight | Meaning |
|---|---|---|
| `strong_articulation` | 1.0 | Correct and complete, may be terse |
| `weak_articulation` | 0.75 | Directionally correct but incomplete |
| `guided_recognition` | 0.5 | Correct only after scaffolding |
| `absent` | None (tiny transit) | KC not engaged this turn |
| `misconception` | 0.0 | Student states a factually wrong belief |
| `contradiction` | 0.0 | Student contradicts a prior correct answer |
| `tangent_initiation` | None (tiny transit) | Student redirects with a question rather than answering — not a knowledge failure |

### Evaluation Metrics

Full design rationale in `docs/evaluation_plan.md`. Summary:

**Composite formula:**
```
score = NAC × (0.5·KFT + 0.25·PR + 0.25·LCQ + MRQ_adjustment)
MRQ_adjustment = 0.15 × (MRQ − 0.5)  if misconceptions detected, else 0.0
```

| Metric | Range | What it measures |
|---|---|---|
| **NAC** | 0–1 | Fraction of tutor turns that are Socratically compliant (no direct answers, no explicit confirmation, no direct correction). Multiplicative wrapper on composite. |
| **KFT** | 0–1 | Fraction of turns targeting a KC at the current knowledge frontier (unmastered, prerequisites met). |
| **PR** | 0–1 | 1 − (stall turns / total turns). A stall = ≥3 consecutive turns on the same KC with no BKT progress, or on a mastered KC. |
| **LCQ** | 0–1 | Fraction of turns where response type (concept/convention/narrative) matches what was warranted. Experimental — unreliable without correct `knowledge_type` in domain map. |
| **MRQ** | 0–1 or null | When misconceptions detected: fraction where tutor probed Socratically rather than ignoring or correcting directly. null when no misconceptions. |
| **TBA** | null | Topic Boundary Adherence — not yet implemented (always null). |

---

## The Evaluation Pipeline

### Entry point: `analyze_transcript(analysis_input, client, compute_nac)`

Located in `tutor_eval/evaluation/analyzer.py`. Takes an `analysis_input` dict and returns
an `EvaluationResult` dataclass (serializable via `.to_dict()`).

**What the analyzer reads from `analysis_input`:**

| Field | Used for |
|---|---|
| `session_id`, `article_id` | Passed through to `EvaluationResult` (identifiers only) |
| `domain_map` | Build KC graph, determine frontier, get `knowledge_type` per KC |
| `bkt_initial_states` | Initialize BKT; falls back to all-absent if empty `{}` |
| `lesson_turns[].role` | Route turns: `"user"` = student, `"tutor"` = tutor |
| `lesson_turns[].content` | Text fed to Haiku classifiers |
| `lesson_turns[].turn_number` | Stored in `TurnResult` |
| `lesson_turns[].reviewer_verdict` | Stored for diagnostics only; no metric effect |

**Fields NOT read:** `article_title`, `assessment_turns`, `lesson_turns[].raw_content`,
`lesson_turns[].tutor_state_snapshot`.

**One Haiku API call per student turn** (observation classifier) +
**one Haiku API call per tutor turn** (NAC/KFT/LCQ/MRQ classifier) = 2 calls per dialogue turn.

### `analysis_input` format

```json
{
  "session_id": "...",
  "article_id": "...",
  "article_title": "...",
  "domain_map": { "core_concepts": [...], "recommended_sequence": [...], ... },
  "bkt_initial_states": {
    "<kc_id>": { "p_mastered": 0.1, "knowledge_class": "absent", "observation_history": [] }
  },
  "assessment_turns": [],
  "lesson_turns": [
    {
      "turn_number": 1,
      "role": "tutor",
      "content": "...",
      "raw_content": "...",
      "reviewer_verdict": null,
      "tutor_state_snapshot": null,
      "evaluator_snapshot": null
    }
  ]
}
```

Role is `"tutor"` or `"user"` (not `"student"` — the ingestion pipeline normalizes this).

---

## CLI Tools

### `score.py` — Score an analysis_input file

```bash
python score.py <analysis_input.json> [--no-nac] [-o result.json]
```

Input: an `analysis_input` JSON (from `ingest.py`, from the webapp API, or hand-crafted).
Output: `EvaluationResult` JSON (stdout or file).

`--no-nac`: sets all `nac_verdict` to `"disabled"` and `nac=1.0`. Skips one Haiku call
per tutor turn. Useful for fast iteration when NAC compliance is not being studied.

### `ingest.py` — Convert a raw transcript to analysis_input

```bash
# Generate analysis_input (inspect before scoring)
python ingest.py transcript.json

# One-shot: generate + score
python ingest.py transcript.json --score -o result.json

# Override domain map
python ingest.py transcript.json --domain-map my_map.json --score
```

See `docs/transcript_analysis.md` for the complete raw transcript format and all CLI flags.

**Raw transcript format (minimum viable):**
```json
{
  "topic": "Extensive form games in game theory",
  "turns": [
    { "role": "tutor",   "content": "..." },
    { "role": "student", "content": "..." }
  ]
}
```

Optional fields: `domain_map` (inline object or file path), `wikipedia_url`, `bkt_preset`
(`"absent"` / `"prereqs_mastered"` / `"all_partial"`), `session_id`, `source`, `date`.

Domain map sources (priority order): inline `domain_map` → `wikipedia_url` → `topic` string.
All generated domain maps are cached in `~/.socratic-domain-cache/`.

### `simulate.py` — Run a session with an external tutor

```bash
python simulate.py config.yaml                         # run session, save transcript
python simulate.py config.yaml --score -o result.json  # run + ingest + score
python simulate.py config.yaml --human                 # human student, LLM tutor
```

Driven by a YAML config file. Produces a raw-transcript-v1 JSON that flows into
`ingest.py` / `score.py` without modification.

**Config file structure:**
```yaml
topic: "Extensive form games in game theory"
domain_map: "a2-domain-map.json"   # optional — generated from topic if absent
source: "gpt-4o"

tutor:
  type: generic_api
  model: gpt-4o
  base_url: null                   # null = OpenAI; set for OpenRouter/Ollama/etc.
  api_key_env: OPENAI_API_KEY      # name of the env var holding the key
  max_tokens: 2048
  temperature: 1.0
  include_domain_map: false        # if true, injects domain map JSON into prompt
  system_prompt: |
    You are a Socratic tutor for {topic}. Never give direct answers.
    Ask questions that guide the student to discover concepts themselves.

student:
  type: llm                        # llm | human
  preset: novice                   # novice | partial_knowledge | expert | misconception_heavy
  base_model: haiku                # haiku | sonnet
  misconception_count: 0           # number of misconceptions to inject from domain map

opening_message: null              # null → "Hi, I'm trying to understand {topic}."
max_turns: 20
min_turns: 8
session_id: null                   # null → auto UUID
output: null                       # null → {session_id}_transcript.json
verbose: true
```

**System prompt substitution keys:** `{topic}` (always replaced), `{domain_map_json}`
(replaced only if `include_domain_map: true`).

**Session termination:** `max_turns` reached, or `[SESSION_COMPLETE]` appears in any
response (flagged as `ended_by: "tutor"` or `"student"` in `_metadata`), or Ctrl-C
(saves partial transcript as `ended_by: "interrupted"`).

---

## Simulation Framework

### `GenericAPITutor` (`tutor_eval/tutors/external.py`)

Wraps any OpenAI-compatible chat completion API as an `AbstractTutor`. Covers:
- **GPT-4o** via OpenAI (`base_url=null`)
- **Open-source models** via OpenRouter, Together.ai, vLLM, Ollama
- **Claude** via LiteLLM (with `base_url` pointing to LiteLLM proxy)

Not covered: direct Anthropic API with extended features (prompt caching, thinking). For
those, use `SocraticTutor` in `tutor_eval/tutors/socratic.py`.

```python
from tutor_eval.tutors.external import GenericAPITutor

tutor = GenericAPITutor(
    model="gpt-4o",
    system_prompt="You are a Socratic tutor for {topic}...",
    base_url=None,           # or "https://openrouter.ai/api/v1"
    api_key="sk-...",        # or reads OPENAI_API_KEY
)
response = tutor.respond(student_message, history)
# history: list of {"role": "student"|"tutor", "text": str}
```

### Student profiles (`tutor_eval/student/domain_profile.py`)

Generates `StudentAgent` profiles from domain maps. The profile dict specifies which KCs
the student has mastered, partially understands, or hasn't encountered.

```python
from tutor_eval.student.domain_profile import generate_profile, build_kg_from_domain_map

profile, kg = generate_profile(
    domain_map,
    preset="novice",           # novice | partial_knowledge | expert | misconception_heavy
    misconception_count=2,     # inject 2 misconceptions from domain_map.common_misconceptions
    base_model="haiku",
)
# profile: {mastered: [...kc_ids], partial: [...], absent: [...], misconceptions: [...]}
# kg:      {kcs: [{id, name}], edges: [{from, to}]}
```

**Preset distributions:**

| Preset | mastered | partial | absent |
|---|---|---|---|
| `novice` | Root KCs (no incoming edges) | — | All non-root KCs |
| `partial_knowledge` | Root KCs + first half of sequence | Midpoint KC | Rest |
| `expert` | All KCs | — | — |
| `misconception_heavy` | Root KCs | First 2 non-root KCs | Rest |

`misconception_count` controls how many entries from `domain_map.common_misconceptions`
are injected. Misconceptions are matched to KCs by word overlap (heuristic). Works for
any preset.

### `run_session()` (`tutor_eval/session.py`)

Orchestrates a session and produces a raw-transcript-v1 dict.

```python
from tutor_eval.session import run_session

transcript = run_session(
    tutor=tutor,               # AbstractTutor
    domain_map=domain_map,
    topic="Extensive form games",
    student_type="llm",        # or "human"
    student_agent=student_agent,
    profile=profile,
    kg=kg,
    max_turns=20,
    source="gpt-4o",
    verbose=True,
    output_file="transcript.json",
)
# transcript is a raw-transcript-v1 dict, ready for ingest.py
```

The output transcript embeds the domain map and BKT initial states inline, so `ingest.py`
requires no API calls to resolve the domain map (free re-scoring).

---

## Webapp

The webapp is a FastAPI application providing a live tutoring interface for Wikipedia articles.
It is not required for offline transcript evaluation. Key entry points:

**Run:** `uvicorn webapp.main:app --reload`

**Key API flows:**
- `POST /api/auth/register` + `POST /api/auth/login` → JWT token
- `GET /api/articles` → article catalog (no auth)
- `POST /api/sessions` → start a session (auth required)
- `POST /api/sessions/{id}/turns` → send a student message, get tutor response
- `GET /api/admin/sessions/{id}/analysis-input` → fetch `analysis_input` JSON for scoring
- `POST /api/export/sessions/{id}/analyze` → trigger async post-hoc scoring (superuser)
- `GET /api/export/sessions/{id}/analysis` → retrieve stored scoring result

See `API.md` for curl-based examples of all workflows.

**Credits system:** Users start with 0 credits. Each tutoring turn costs 1 credit. Superusers
are exempt. Add credits via `POST /api/admin/users/{id}/credits`.

**Domain map for webapp sessions:** Generated by the webapp when an article is first loaded,
stored in the database. Two passes: `compute_domain_map()` (Sonnet) then `enrich_domain_map()`
(Sonnet). Cached in DB by article ID.

---

## Implementation Status

### Complete

- `analyze_transcript()` with NAC, KFT, PR, LCQ, MRQ, composite metrics
- Per-turn `TurnResult` with BKT snapshots, observation history, stall detection
- BKT observation classifier (Haiku) with 7 observation classes including `tangent_initiation`
- Raw transcript ingestion pipeline (`ingest.py`, `tutor_eval/ingestion/`)
- Domain map normalization for multiple input formats (webapp, phase-structured, KG, flat list)
- Domain map generation from topic string or Wikipedia URL (2-pass: structure + enrichment)
- Domain map caching at `~/.socratic-domain-cache/`
- Student profile generation from domain maps with presets and misconception injection
- `GenericAPITutor` adapter for OpenAI-compatible APIs
- `run_session()` with LLM and human student modes
- `simulate.py` CLI with YAML config
- Webapp with full tutoring flow, JWT auth, credits, domain map graph
- Webapp async post-hoc analysis endpoints (`export.py`)
- Full documentation in `docs/transcript_analysis.md` and `docs/evaluation_plan.md`

### Partially Complete / Experimental

- **LCQ**: Implemented but experimental. Accuracy depends on `knowledge_type` annotations in the
  domain map. When `knowledge_type` is absent or defaults to `"concept"`, LCQ scores are
  unreliable. Calibration against human-rated transcripts has not been done.
- **Observation classifier robustness**: The bias toward length over conceptual accuracy has been
  partially addressed (revised class definitions in `bkt.py`), but edge cases remain (e.g.,
  single-sentence technical answers by domain experts).

### Not Yet Implemented

- **TBA (Topic Boundary Adherence)**: Always `null`. Requires `session_state()` data from the
  `SocraticTutor`, which is not available for external tutor transcripts. Excluded from composite.
- **Anthropic-native external tutor adapter**: `GenericAPITutor` uses the OpenAI SDK. A direct
  `AnthropicAPITutor` class (using the `anthropic` SDK with prompt caching, extended thinking, etc.)
  would allow testing Claude with custom system prompts outside of `SocraticTutor`.
- **Batch simulation**: No CLI support for running N student profiles against a tutor and
  aggregating results. Currently requires N manual `simulate.py` invocations.
- **Tutor comparison workflow**: No built-in tooling for running the same student profile against
  multiple tutors and comparing scores. Would need a wrapper script.
- **Database integration for external transcripts**: `POST /api/export/sessions/{id}/analyze`
  only works for sessions in `webapp.db`. External transcripts evaluated via `score.py` cannot
  be stored or browsed through the webapp.
- **Multi-turn assessment**: The pre-session assessment flow in the webapp initializes BKT L0
  values. External transcripts fall back to `bkt_preset`. A tool for running a brief programmatic
  assessment before a simulated session is not implemented.
- **LCQ calibration**: The LCQ warranted-type thresholds (`p < 0.5` → convention/narrative warranted)
  are heuristic. No calibration against human judgment has been done.

---

## Known Limitations

**BKT initialization for external transcripts:**
All non-webapp transcripts default BKT initial states from a preset (`absent`, `prereqs_mastered`,
or `all_partial`). For sessions where the student has significant prior knowledge, `absent`
underestimates starting knowledge, causing KCs to be marked as mastered early and subsequent
appropriate questions to score as KFT violations. Use `prereqs_mastered` for course-enrolled
students or provide explicit `bkt_initial_states`.

**Domain map quality directly controls metric quality:**
A poorly-specified domain map produces misleading scores. Specifically:
- Missing `knowledge_type` → all warranted_types default to "concept" → LCQ unreliable
- Missing `prerequisite_for` edges → BKT frontier is flat → KFT and PR less meaningful
- Domain map topic mismatch → most turns score `off_map` → KFT ≈ 0

The two-stage workflow (`ingest.py` without `--score`, inspect `analysis_input.json`, then
`score.py`) is recommended for new topics to verify domain map quality before scoring.

**Homework review vs. Socratic lesson:**
The metrics assume pure Socratic intent. A homework review session — where some direct
explanation is appropriate — will show artificially high NAC violations. The composite score
is not meaningful for such sessions without contextual interpretation.

**`tangent_initiation` observation:**
When a student asks a clarifying question rather than answering, BKT is not updated (no
knowledge signal). This is correct behavior, but the observation is recorded in
`observation_history` for diagnostics. It does not prevent the KFT classifier from still
identifying the tutor's next turn KC, so KFT remains unaffected.

---

## Future Work Priorities

In rough priority order:

1. **Batch simulation CLI**: `simulate.py --batch profiles.yaml` — runs N profiles against a
   tutor, saves N transcripts, aggregates scores. Needed for systematic tutor comparison.

2. **Tutor comparison script**: Given a domain map and a set of tutor configs, run the same
   student profile against each, score all, and produce a comparison table. Core use case
   for the research agenda.

3. **LCQ calibration**: Collect a set of hand-rated transcripts and tune the warranted-type
   thresholds. Until this is done, LCQ should be treated as diagnostic, not scored.

4. **`AnthropicAPITutor`**: A `GenericAPITutor` variant that uses the `anthropic` SDK directly.
   Enables testing Claude with custom system prompts, prompt caching, and extended thinking
   without going through LiteLLM.

5. **Structured session config**: A way to specify the student's actual knowledge profile
   (not just a preset) — e.g., a YAML file listing mastered/partial/absent KCs by name
   for a specific student, to be used as `bkt_initial_states` in the transcript.

6. **TBA metric**: Requires extracting the `session_state()` data from `SocraticTutor` and
   comparing it against an independent post-hoc topic classifier. Not straightforward for
   external tutors.

7. **Database import for external transcripts**: `POST /api/import/transcript` endpoint that
   creates a session row from a raw transcript JSON and triggers analysis, enabling webapp
   browsing of externally-generated session results.

8. **Human tutor adapter**: An `AbstractTutor` subclass that reads responses from stdin, for
   cases where a human is playing the tutor role and an LLM agent plays the student.

---

## Key Design Decisions (Non-Obvious)

**BKT is retrospective, not live:** BKT runs post-hoc over the transcript. The tutor does not
have access to BKT estimates during the session. The tutor's own `session_state()` is used for
TBA; BKT is used only for KFT, PR, and MRQ evaluation.

**NAC is a multiplicative wrapper:** A tutor that violates the Socratic constraint on 40% of
turns is not a "mediocre Socratic tutor" — it fails a precondition. Folding NAC into a weighted
average allows it to be compensated by high KFT, which is misleading. The multiplicative design
ensures persistent violations depress the entire score.

**Domain map normalization is lossy but safe:** The normalizer converts any reasonable domain
map format to the webapp format. If `knowledge_type` is absent, it defaults to `"concept"` and
a warning is emitted. The evaluation proceeds rather than failing; the caller is expected to
inspect the result.

**Role normalization in converter:** `analyze_transcript()` expects role `"user"` (not
`"student"`) for the student side. The converter in `tutor_eval/ingestion/converter.py`
normalizes `"student"` → `"user"` before building the `analysis_input`. The analyzer itself
is never changed.

**`tangent_initiation` is in `OBSERVATION_CLASSES` but not in `valid_classes - {"absent"}`:**
Wait, it IS in `valid_classes` — `valid_classes = set(OBSERVATION_CLASSES) - {"absent"}`. Only
`absent` is filtered from classifier output. `tangent_initiation` IS returned when detected
(unlike `absent` which is never returned), so it appears in `observation_history` and is
visible in evaluation diagnostics.

**Session runner produces raw-transcript-v1, not JSONL:** The new `run_session()` in
`tutor_eval/session.py` is evaluation-free. No BKT runs during the session. The old
`run_simulation()` in `tutor_eval/simulation.py` runs BKT live and outputs JSONL — it is
preserved but considered legacy for new use cases.
