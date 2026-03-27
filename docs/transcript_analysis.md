# Transcript Analysis Pipeline

This document covers everything needed to evaluate a conversation transcript using the
post-hoc evaluation pipeline: the raw transcript format, the `ingest.py` and `score.py`
CLI tools, domain map generation, and a summary of the evaluation metrics.

For the full metric design rationale see `docs/evaluation_plan.md`.

---

## Overview

The evaluation pipeline measures the quality of a Socratic tutoring session by replaying
the transcript through a Bayesian Knowledge Tracing (BKT) model and applying a set of
classifiers to each turn. All evaluation is **post-hoc** — nothing runs during the session.

Two input paths exist:

| Path | Source | Entry point |
|---|---|---|
| **Raw transcript** | Any tutor (CSV export, LLM API log, manual transcript) | `ingest.py` → `score.py` |
| **Webapp session** | Live sessions stored in `webapp.db` | `POST /api/export/sessions/{id}/analyze` or `score.py` with fetched JSON |

This document covers the raw transcript path. For the webapp path see `API.md`.

---

## Quick Start

```bash
# Activate the virtual environment
source .venv/bin/activate

# One-shot: ingest and score immediately
python ingest.py my_transcript.json --score -o result.json

# Two-stage: inspect the domain map before scoring (recommended for new topics)
python ingest.py my_transcript.json
# review my_transcript_analysis_input.json — check KC names and graph structure
python score.py my_transcript_analysis_input.json -o result.json
```

Both commands require `ANTHROPIC_API_KEY` to be set in the environment for domain map
generation (and for scoring, unless `--no-nac` is used). Domain maps are cached —
subsequent runs on the same topic cost nothing.

---

## Raw Transcript Format

Save as a `.json` file. The `_schema` field is optional but recommended for version tracking.

```json
{
  "_schema": "raw-transcript-v1",

  "topic": "Finitely repeated games in game theory",

  "turns": [
    { "role": "tutor",   "content": "What happens in the last round of a finitely repeated prisoner's dilemma?" },
    { "role": "student", "content": "Both players defect, because there's no future to cooperate for." },
    { "role": "tutor",   "content": "Right — so what does that imply about the second-to-last round?" }
  ],

  "bkt_preset": "prereqs_mastered",

  "session_id": "study-session-2026-03-15",
  "source":     "gpt-4o",
  "date":       "2026-03-15"
}
```

### Field Reference

| Field | Required | Description |
|---|---|---|
| `topic` | **Yes** | Free-text description of the lesson topic. Used as input to domain map generation when no other source is provided. Be specific — `"Nash equilibrium in extensive-form games"` produces a better KC graph than `"game theory"`. |
| `turns` | **Yes** | Ordered list of conversation turns. Each turn must have `role` (`"tutor"` or `"student"`) and `content` (non-empty string). |
| `domain_map` | No | Inline domain map object or path to a domain map JSON file. If provided, it is normalized and used directly — no API calls are made for domain map generation. Takes precedence over `wikipedia_url`. |
| `wikipedia_url` | No | URL of a Wikipedia article to use as the knowledge source. The article text is fetched and passed to the domain mapper. Takes precedence over `topic`-string generation. |
| `bkt_preset` | No | BKT initialization strategy when no assessment data is available. See [BKT Initialization Presets](#bkt-initialization-presets). Default: `"absent"`. |
| `bkt_initial_states` | No | Explicit per-KC BKT states in the same format as `GET /api/admin/sessions/{id}/analysis-input`. When present, overrides `bkt_preset`. |
| `session_id` | No | Label for the session. Appears in evaluation output. Defaults to a random UUID. |
| `source` | No | Identifier for the tutor system (e.g., `"gpt-4o"`, `"human-tutor"`). Stored in output as `article_id`. |
| `date` | No | ISO 8601 date string. Not used in scoring; stored for provenance. |

### Minimum viable transcript

```json
{
  "topic": "Backward induction in extensive-form games",
  "turns": [
    { "role": "tutor",   "content": "Where do you start when applying backward induction?" },
    { "role": "student", "content": "At the last decision node." }
  ]
}
```

A transcript with fewer than 8 tutor turns will produce `is_valid: false` in the output —
scores exist but are flagged as unreliable.

---

## ingest.py

Converts a raw transcript to `analysis_input` format (and optionally scores it).

```
python ingest.py <transcript.json> [options]
```

### Options

| Option | Default | Description |
|---|---|---|
| `--output-input FILE` | `<stem>_analysis_input.json` | Path to write the generated analysis_input JSON. |
| `--score` | off | Run `analyze_transcript()` immediately after generating analysis_input. |
| `--no-nac` | off | Disable NAC when scoring — skips one Haiku API call per tutor turn. Useful for fast iteration; sets `nac=1.0` in output. |
| `-o FILE`, `--output-result FILE` | stdout | Path to write scoring result JSON. Only meaningful with `--score`. |
| `--cache-dir DIR` | `~/.socratic-domain-cache` | Directory for cached domain maps. |
| `--domain-map FILE` | — | Load domain map from FILE, overriding any `domain_map` or `wikipedia_url` in the transcript. |
| `--no-enrich` | off | Skip the enrichment pass (Pass 2) when generating a domain map. Faster, but `knowledge_type` defaults to `"concept"` for all KCs, which reduces LCQ accuracy. |

### Workflow examples

```bash
# Inspect domain map before committing to a full scoring run
python ingest.py lecture_transcript.json
cat lecture_transcript_analysis_input.json | python3 -m json.tool | grep -A3 '"core_concepts"'

# Score without NAC (no API calls for Haiku — much faster)
python ingest.py lecture_transcript.json --score --no-nac -o result.json

# Reuse a domain map from a previous run on the same topic
python ingest.py session2.json --domain-map session1_analysis_input.json --score

# Override domain map with a hand-crafted file
python ingest.py transcript.json --domain-map my_custom_domain_map.json --score -o result.json
```

---

## score.py

Scores an `analysis_input` JSON file directly — no raw transcript ingestion.

```
python score.py <analysis_input.json> [--no-nac] [-o output.json]
```

Use `score.py` when you already have a correctly formatted `analysis_input` file, either
from `ingest.py`, from `GET /api/admin/sessions/{id}/analysis-input`, or from a previous
run you want to re-score.

```bash
# Re-score an already-generated analysis_input with NAC disabled
python score.py my_transcript_analysis_input.json --no-nac -o result_no_nac.json

# Score a webapp session fetched via the API
curl -s http://localhost:8000/api/admin/sessions/<id>/analysis-input \
  -H "Authorization: Bearer $TOKEN" > session.json
python score.py session.json -o result.json
```

---

## Domain Map

The domain map is a structured representation of the knowledge components (KCs) in the
lesson topic, their prerequisite relationships, and their types. It is the foundation of
all evaluation metrics — KFT, PR, LCQ, and MRQ all depend on it.

### Generation

When no inline domain map is provided, `ingest.py` generates one using two LLM passes:

**Pass 1 — Structure** (`compute_domain_map`): Calls Claude Sonnet to identify KCs,
prerequisite edges, common misconceptions, and a recommended teaching sequence.

**Pass 2 — Enrichment** (`enrich_domain_map`): A second Sonnet call annotates each KC
with `knowledge_type` (`concept`, `convention`, or `narrative`) and `reference_material`.
The `knowledge_type` is used by the LCQ metric to classify expected tutor behavior. Skip
Pass 2 with `--no-enrich` for faster runs at some cost to LCQ accuracy.

Total generation time: 30–90 seconds. The result is cached — subsequent runs on the same
topic are instant.

### Caching

Generated domain maps are cached as JSON files under `~/.socratic-domain-cache/`:

| Source | Cache key |
|---|---|
| `topic` string | `{slug-of-topic}.json` |
| `wikipedia_url` | `wiki-{slug-of-title}.json` |

If a cached file exists but was generated without enrichment (no `knowledge_type` fields),
`ingest.py` automatically runs Pass 2 and overwrites the cache.

To force regeneration (e.g., if the topic description has changed significantly):
```bash
rm ~/.socratic-domain-cache/{slug}.json
python ingest.py transcript.json
```

### Normalization

Pre-existing domain maps from external sources may not match the webapp's exact JSON
structure. `ingest.py` normalizes the following formats automatically:

| Format | Detection | Normalization |
|---|---|---|
| Webapp format | `"core_concepts"` key present | Fills missing `knowledge_type` (defaults to `"concept"`), fills missing `recommended_sequence`, accepts `"name"` or `"title"` as aliases for `"concept"` |
| KG format | `"kcs"` + `"edges"` keys present | Converts to `core_concepts` format; `knowledge_type` defaults to `"concept"` |
| Flat string list | List under `"concepts"`, `"knowledge_components"`, `"topics"`, or `"items"` | Wraps each string as a concept with no prerequisites |

When providing an external domain map, the `knowledge_type` field on each concept
significantly affects LCQ scores. If it is absent, all KCs are treated as `"concept"`
(pure Socratic), which is conservative — the tutor is evaluated as if every KC should
be taught through questioning alone.

**Knowledge type definitions:**
- `"concept"` — the student should be able to reason to the answer (no reference material needed)
- `"convention"` — a defined rule or fact that must be stated first, then applied
- `"narrative"` — structured content (a framework, a list of principles) that must be presented, then reasoned about

---

## BKT Initialization Presets

When no pre-session assessment data is available (the typical case for external
transcripts), BKT initial states must be estimated. The `bkt_preset` field controls this.

| Preset | p_mastered per KC | When to use |
|---|---|---|
| `"absent"` (default) | 0.10 for all KCs | Unknown prior knowledge; conservative. BKT self-corrects within 2–3 turns of evidence. |
| `"prereqs_mastered"` | 0.90 for root KCs (no incoming edges); 0.10 for all others | Student has background knowledge but not the lesson-specific content. Matches the evaluation plan's recommended fallback. |
| `"all_partial"` | 0.50 for all KCs | Student has encountered the topic before but mastery is uncertain. |

Root KCs are identified structurally: a KC is a root if no other KC lists it in its
`prerequisite_for` field. This computation is purely graph-structural and works on any
normalized domain map.

**Recommendation:** Use `"prereqs_mastered"` for university-level coursework where
foundational knowledge (notation, basic definitions) can be assumed. Use `"absent"` when
evaluating introductory-level sessions or when the student's background is genuinely unknown.

---

## Output Format

Both `ingest.py --score` and `score.py` produce the same JSON output:

```json
{
  "session_id": "...",
  "article_id": "...",
  "nac":              0.94,
  "kft":              0.71,
  "pr":               0.88,
  "lcq":              0.60,
  "mrq":              1.0,
  "mrq_adjustment":   0.075,
  "composite":        0.74,
  "total_tutor_turns": 18,
  "is_valid":         true,
  "invalidity_reason": null,
  "reviewer_active":  false,
  "reviewer_rewrite_count": 0,
  "turn_results": [ ... ]
}
```

`is_valid` is `false` when `total_tutor_turns < 8`. Scores are computed but should be
treated as indicative only.

`reviewer_active` is `false` for all external transcripts (no response reviewer was
running). `nac` is computed independently by the post-hoc classifier regardless.

### Metric Summary

| Metric | Range | Measures |
|---|---|---|
| `nac` | 0–1 | Fraction of tutor turns that did not directly answer the question, confirm correctness, or correct the student. Acts as a **multiplicative wrapper** on the composite. |
| `kft` | 0–1 | Fraction of tutor turns targeting a KC that is on the current knowledge frontier (unmastered, prerequisites met). |
| `pr` | 0–1 | Fraction of turns not spent in an unproductive stall (same KC repeated ≥3 times without BKT progress, or on an already-mastered KC). |
| `lcq` | 0–1 | Fraction of tutor turns where the response type (Socratic / direct provision / scaffolded) matched what was appropriate given the KC type and student's current mastery. |
| `mrq` | 0–1 or null | When misconceptions were detected: fraction of those turns where the tutor probed the misconception Socratically rather than ignoring or directly correcting it. `null` when no misconceptions arose. |
| `mrq_adjustment` | −0.075 to +0.075 | Additive composite adjustment: `0.15 × (mrq − 0.5)` when `mrq` is not null. |
| `composite` | 0–1 | `nac × (0.5·kft + 0.25·pr + 0.25·lcq + mrq_adjustment)` |

`lcq` is currently included in the composite but considered experimental pending
calibration. See `docs/evaluation_plan.md` § LCQ for the full design.

### Per-turn data (`turn_results`)

Each entry in `turn_results` corresponds to one tutor turn:

```json
{
  "turn_number":          5,
  "targeted_kc_id":       "backward-induction",
  "kc_status":            "on_frontier",
  "nac_verdict":          "compliant",
  "reviewer_verdict":     null,
  "observed_type":        "concept",
  "warranted_type":       "concept",
  "mrq_verdict":          null,
  "is_stall_turn":        false,
  "stall_shape":          null,
  "bkt_snapshot":         { "backward-induction": 0.31, "subgame-perfection": 0.10 },
  "preceding_observations": [
    {
      "kc_id":             "backward-induction",
      "observation_class": "weak_articulation",
      "evidence_quote":    "At the last decision node."
    }
  ]
}
```

`kc_status` values: `on_frontier`, `mastered`, `prereqs_not_met`, `off_map`.

`observation_class` values in `preceding_observations`: `strong_articulation`,
`weak_articulation`, `guided_recognition`, `misconception`, `contradiction`,
`tangent_initiation` (student asked a question rather than answering — no BKT update).

---

## Cost and Runtime

Approximate API usage per session (using Claude Haiku for classifiers, Sonnet for domain map):

| Operation | Model | Calls | Notes |
|---|---|---|---|
| Domain map generation (Pass 1) | Sonnet | 1 | ~10–30s; cached after first run |
| Domain map enrichment (Pass 2) | Sonnet | 1 | ~10–30s; cached after first run |
| Student observation classifier | Haiku | 1 per student turn | ~0.5s each |
| Tutor turn classifier (NAC, KFT, LCQ, MRQ) | Haiku | 1 per tutor turn | ~0.5s each |

For a 20-turn session (10 student + 10 tutor):
- First run: ~3–5 min (domain map + 20 Haiku calls)
- Re-runs (cached domain map): ~30–60s (20 Haiku calls)
- `--no-nac` has no effect on call count — NAC is bundled into the per-turn tutor classifier

To minimize cost during iteration, run `--no-nac` only when you want to skip the entire
tutor-turn classification (set `nac=1.0`). For the student classifier (BKT observations),
there is no skip option — those classifications drive all other metrics.

---

## File Structure

```
ingest.py                          CLI: raw transcript → analysis_input (→ optional scoring)
score.py                           CLI: analysis_input → evaluation result

tutor_eval/ingestion/
    schema.py                      validate_raw_transcript() → (errors, warnings)
    domain_resolver.py             resolve_domain_map() — generation, normalization, caching
    converter.py                   prepare_analysis_input() — turn normalization, BKT presets

tutor_eval/evaluation/
    analyzer.py                    analyze_transcript() — BKT replay + Haiku classifiers
    bkt.py                         BKTEvaluator, observation classifier, BKT update rules
    metrics.py                     compute_nac/kft/pr/lcq/mrq/composite

~/.socratic-domain-cache/          Generated domain maps (JSON files, keyed by topic slug)
```
