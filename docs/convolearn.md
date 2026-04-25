# ConvoLearn Evaluation Pipeline

**Location:** `convolearn/`
**Purpose:** Validate the Socratic tutor evaluation framework against an external benchmark by
running `analyze_transcript()` on real human tutor–student dialogues from the ConvoLearn dataset,
then comparing resulting metric scores against ConvoLearn's own ground-truth effectiveness and
completeness ratings.

---

## Quick Start

```bash
source .venv/bin/activate

# Step 1 — Inspect which prompts will be selected (no API calls, instant)
python -m convolearn.score_batch --parse-only --sample-size 7

# Step 2 — Validate cheaply: score 3 dialogues per prompt using the saved sample
python -m convolearn.score_batch \
  --from-sample \
  --max-dialogues-per-prompt 3 \
  --no-nac --no-lcq \
  --output-dir convolearn/results/

# Step 3 — Full production run once you're satisfied with the sample
python -m convolearn.score_batch \
  --dataset masharma/convolearn \
  --sample-size 7 \
  --no-nac --no-lcq \
  --output-dir convolearn/results/
```

All five stages run automatically inside a single command. No pre-processing or
separate batch-creation step is required — but the `--parse-only` and `--from-sample`
flags let you inspect and iterate cheaply before committing to a full scoring run.

---

## Dataset

**Source:** HuggingFace — `masharma/convolearn`
**Format:** Parquet, loaded via the HuggingFace `datasets` library
**Size:** 2,134 rows, single `train` split
**Content:** Earth Science tutor–student dialogues, each annotated with ground-truth ratings

| Field | Type | Description |
|---|---|---|
| `earthscience_topic` | string (4 values) | Broad Earth Science category |
| `kb_dim` | string (6 values) | Knowledge-building dimension |
| `kb_subdim` | string (21 values) | Knowledge-building subdimension |
| `effectiveness_consensus` | float64 | Pedagogical effectiveness (1–5) |
| `completeness_consensus` | float64 | Conversation completeness (1–3) |
| `cleaned_conversation` | string | Full dialogue (see format below) |
| `num_exchanges` | int64 | Number of back-and-forth exchange pairs (2–13) |

**Conversation format:**
```
Student: [opening question]
Teacher: [response]
Student: [follow-up]
...
```

The pipeline uses the **first Student utterance** as the grouping key (`question_prompt`).
Dialogues sharing the same opening question are directly comparable and constitute one prompt group.

---

## How the Pipeline Works

The pipeline runs five sequential stages inside a single CLI command. **All batch creation is
automatic** — you do not need to pre-process the dataset or manually create a sample file
before scoring.

### Stage 1 — Parse & Sample (`convolearn/parse.py`)

Downloads the dataset (HuggingFace cache after first run), extracts each row's first Student
utterance, groups rows by that question prompt, and draws a reproducible random sample.

**Filtering logic:**
1. For each question prompt group, keep only rows whose `cleaned_conversation` parses to
   at least `--min-messages` non-empty turns. This count is computed after normalization
   (multi-line responses merged, empty-content turns dropped), so it reflects the actual
   message count that `analyze_transcript()` will see — not the dataset's `num_exchanges`
   metadata field, which can overcount.
2. Keep only groups with ≥ `--min-dialogues` qualifying rows.
3. Randomly sample up to `--sample-size` prompts (seed-controlled via `--seed`).

With default settings on ConvoLearn, 46 prompts qualify; 7 are selected.

**Output:** `sampled_dialogues.json`

### Stage 2 — Domain Map Generation (`convolearn/domain_maps.py`)

For each unique question prompt in the sample, generates a domain map using the question
text as the topic. The generation mode depends on `--domain-source`:

- **`sentence` (default):** single-pass generation targeting 4–7 KCs, no enrichment. Maps
  are cached at `~/.socratic-domain-cache/` with a `-slim` key suffix. Then trimmed to
  `depth_priority == "essential"` KCs (up to 7) with non-evaluation fields stripped.
  Costs **1 Sonnet call** per prompt.
- **`article`:** two-pass generation (structure + enrichment) targeting 12–20 KCs — matches
  the webapp's full domain maps. Costs **2 Sonnet calls** per prompt.

**Re-running on the same prompts with the same `--domain-source` costs nothing after the first run.**

**Output:** `domain_maps.json`

### Stage 3 — Transcript Adaptation (`convolearn/adapter.py`)

Converts each ConvoLearn dialogue into the `analysis_input` format that `analyze_transcript()`
expects. The adapter:

1. Parses `cleaned_conversation` into `{role, content}` turns, handling multi-line Teacher
   responses (a turn spans lines until the next role label).
2. Normalises roles: `Teacher` → `tutor`, `Student` → `student` (then `student` → `user`
   in `converter.py`).
3. Calls `prepare_analysis_input()` from `tutor_eval/ingestion/converter.py` to produce
   the final `analysis_input`, including BKT initial states from the selected preset.

**Session IDs** follow the pattern `{prompt_id}_{dialogue_idx}`, e.g.
`why-does-the-equator-receive-more-solar-energy_3`.

This stage runs in-memory — no intermediate file is written.

### Stage 4 — Batch Scoring (`convolearn/score_batch.py`)

Calls `analyze_transcript()` on each adapted transcript using a `ThreadPoolExecutor` with
`--workers` threads (default 4). Progress is printed as each dialogue completes.

For each result, a flat record is assembled with all metric scores and the original
ConvoLearn ground-truth ratings attached. Metrics disabled via flags emit `null`
(distinguishable from a computed value of 0.0 or 1.0).

**Output:** `scored_results.json`

### Stage 5 — Aggregation (`convolearn/score_batch.py`)

Computes per-prompt means for all metrics and ConvoLearn ratings. `null` values (from
disabled metrics or sessions with no misconceptions) are excluded from means.

**Output:** `summary.json`

---

## Output Files

All outputs land in `--output-dir` (default `convolearn/results/`, gitignored).

### `sampled_dialogues.json`

One object per sampled prompt:
```json
[
  {
    "prompt_id": "why-does-the-equator-receive-more-solar-energy",
    "question_prompt": "Why does the equator receive more solar energy than the polar regions?",
    "earthscience_topic": "Earth's Energy",
    "dialogues": [
      {
        "dialogue_idx": 0,
        "cleaned_conversation": "Student: Why does...\nTeacher: ...",
        "effectiveness_consensus": 4.0,
        "completeness_consensus": 2.5,
        "num_exchanges": 11
      }
    ]
  }
]
```

### `domain_maps.json`

```json
{
  "why-does-the-equator-receive-more-solar-energy": {
    "topic": "...",
    "core_concepts": [...],
    "recommended_sequence": [...],
    "common_misconceptions": [...]
  }
}
```

### `scored_results.json`

One flat record per dialogue:
```json
[
  {
    "session_id": "why-does-the-equator_3",
    "prompt_id": "why-does-the-equator",
    "earthscience_topic": "Earth's Energy",
    "nac": 0.85,
    "kft": 0.62,
    "pr": 0.90,
    "lcq": 0.70,
    "mrq": null,
    "composite": 0.72,
    "is_valid": true,
    "total_tutor_turns": 11,
    "effectiveness_consensus": 4.0,
    "completeness_consensus": 2.5,
    "num_exchanges": 11,
    "error": null
  }
]
```

`null` for `nac` or `lcq` means that metric was disabled (not measured).
`null` for `mrq` means no misconceptions were detected in this session (not a failure).
`is_valid: false` when `total_tutor_turns < 8` — scores are unreliable for short sessions.
`error` is non-null if the session threw an exception; the remaining metric fields will also
be `null`.

### `summary.json`

One record per sampled prompt with per-prompt means:
```json
[
  {
    "prompt_id": "why-does-the-equator",
    "question_prompt": "Why does the equator receive more solar energy...",
    "n_dialogues": 40,
    "mean_nac": 0.72,
    "mean_kft": 0.58,
    "mean_pr": 0.81,
    "mean_lcq": null,
    "mean_mrq": 0.60,
    "mean_composite": 0.63,
    "mean_effectiveness_consensus": 3.4,
    "mean_completeness_consensus": 2.3
  }
]
```

`null` means either the metric was disabled for this run or no qualifying values existed
(e.g., `mean_mrq` when no session had misconceptions).

---

## Configuration Reference

| Flag | Default | Description |
|---|---|---|
| `--dataset` | `masharma/convolearn` | HuggingFace dataset name |
| `--sample-size N` | `7` | Max number of unique question prompts to sample |
| `--min-dialogues N` | `20` | Min qualifying dialogues required per prompt |
| `--min-messages N` | `10` | Min actual parsed tutor turns per dialogue (post-normalization) |
| `--seed N` | `42` | RNG seed for reproducible prompt sampling |
| `--nac` / `--no-nac` | `--nac` | Enable/disable NAC scoring. `--no-nac` → `nac: null` |
| `--lcq` / `--no-lcq` | `--lcq` | Enable/disable LCQ in output. `--no-lcq` → `lcq: null` |
| `--initial-knowledge` | `tabula_rasa` | BKT prior preset for all sessions |
| `--domain-source` | `sentence` | `sentence` (default): compact 4–7 KC map, no enrichment — calibrated to brief Q&A sessions. `article`: full 12–20 KC enriched map. |
| `--workers N` | `4` | ThreadPoolExecutor parallelism for Stage 4 |
| `--output-dir PATH` | `convolearn/results/` | Directory for all output files |
| `--parse-only` | off | Run Stage 1 only: sample the dataset, print a summary, and exit. **No API calls.** |
| `--from-sample` | off | Skip Stage 1: load `sampled_dialogues.json` from `--output-dir`. Also skips Stage 2 if `domain_maps.json` already exists. |
| `--max-dialogues-per-prompt N` | no limit | Cap the number of dialogues scored per prompt (taken in index order). Applied to the work list only — `sampled_dialogues.json` on disk is never modified. |
| `--append` | off | Append new scores to an existing `scored_results.json` rather than overwriting. Already-scored session IDs are skipped automatically. The summary is recomputed over all combined results after each run. |

### `--initial-knowledge` values

| Value | BKT prior | When to use |
|---|---|---|
| `tabula_rasa` (alias `absent`) | All KCs at p=0.10 | Default. Student has no prior knowledge. |
| `prereqs_mastered` | Root KCs at p=0.90, others p=0.10 | Student has foundational knowledge but not the lesson content. |
| `all_partial` | All KCs at p=0.50 | Student has partial familiarity with most concepts. |

ConvoLearn has no pre-session assessment data, so `tabula_rasa` (the default) is the
appropriate choice. Using `prereqs_mastered` would be reasonable if students in the dataset
are known to have Earth Science prerequisites.

### `--nac` and `--lcq` flags

Both flags control **whether the metric appears in the output** — they do not reduce the number
of Haiku API calls. NAC and LCQ are both computed inside the same per-tutor-turn classifier
call. Disabling them sets the output field to `null` so the analysis phase can distinguish
"not measured" from a real score.

Use `--no-nac --no-lcq` when you want to run a quick scan to check domain map quality (KFT
and PR are pure-Python, cost-free) or when you want to measure NAC and LCQ calibration
separately across multiple runs.

---

## Recommended Workflow

### Inspect the sample before scoring

`--parse-only` runs Stage 1 only. It downloads the dataset (cached by HuggingFace
after first run), groups rows, applies the filter, samples, and prints a summary.
**Zero Anthropic API calls.**

```bash
python -m convolearn.score_batch \
  --parse-only \
  --sample-size 7 \
  --min-messages 10 \
  --seed 42 \
  --output-dir convolearn/results/
```

Example output:
```
=== Stage 1: Parse & Sample ===
[parse] 46 prompts meet criteria (>=20 dialogues with >=10 exchanges)
[parse] Sampled 7 prompts

=== Selected prompts ===
  1. [how-do-ocean-currents-like-the-gulf-stream-affect-the-climate-of-nearby-coas]
     Topic : Earth's Energy
     Question: How do ocean currents like the Gulf Stream affect the climate of nearby...
     Dialogues: 38
  2. [why-does-the-equator-receive-more-solar-energy-than-the-polar-regions]
     ...

Total: 7 prompts, 261 dialogues
Sample written to: convolearn/results/sampled_dialogues.json

(--parse-only: stopping before domain map generation and scoring)
```

Adjust `--sample-size`, `--seed`, or filter flags until you're happy with the
selection. The sample is saved to `sampled_dialogues.json` for the next step.

### Validate cheaply before a full run

Once you have a sample you like, score just a few dialogues per prompt to check
that domain maps are reasonable and scores look sensible:

```bash
python -m convolearn.score_batch \
  --from-sample \
  --max-dialogues-per-prompt 3 \
  --no-nac --no-lcq \
  --workers 2 \
  --output-dir convolearn/results/
```

`--from-sample` loads `sampled_dialogues.json` from disk and skips Stage 1 entirely.
If `domain_maps.json` already exists it also skips Stage 2. With 7 prompts × 3
dialogues = 21 scoring calls this typically takes 2–5 minutes.

Inspect `convolearn/results/scored_results.json` — check that `total_tutor_turns`
looks reasonable (≥ 8 for valid sessions), that `kft` is not uniformly 0 (which
would indicate a domain map topic mismatch), and that no sessions failed.

### Accumulate results across multiple runs

Use `--append` to add new scores to an existing `scored_results.json` without
re-scoring what's already there. Session IDs are deduplicated automatically.

```bash
# Run 1: score 5 dialogues per prompt
python -m convolearn.score_batch \
  --from-sample --append \
  --max-dialogues-per-prompt 5 \
  --no-nac --no-lcq \
  --output-dir convolearn/results/
# → scored_results.json now has 35 records (7 prompts × 5)

# Run 2: score 5 more (indices 5–9)
python -m convolearn.score_batch \
  --from-sample --append \
  --max-dialogues-per-prompt 10 \
  --no-nac --no-lcq \
  --output-dir convolearn/results/
# → scored_results.json now has 70 records (5 new per prompt, 5 skipped)

# Run 3: score the rest — no cap means all remaining dialogues
python -m convolearn.score_batch \
  --from-sample --append \
  --no-nac --no-lcq \
  --output-dir convolearn/results/
# → scored_results.json now has all ~261 records
```

`--max-dialogues-per-prompt` is applied to the **work list only** — it never
modifies `sampled_dialogues.json` on disk, so the full sample is always available
for future runs. `summary.json` is recomputed over all combined results after
each `--append` run.

### Full production run in one shot

```bash
python -m convolearn.score_batch \
  --from-sample \
  --no-nac --no-lcq \
  --workers 4 \
  --output-dir convolearn/results/
```

Using `--from-sample` preserves the exact same prompt selection as your validation
run. Remove `--from-sample` only if you want to re-sample from scratch.

---

## Re-running on an Existing Sample

To re-score the same set of dialogues with different flags (e.g., switching from `--no-nac`
to `--nac`), the sampled dialogues and domain maps from the first run are already saved in
`--output-dir`. You can load them directly via the Python API:

```python
import json, anthropic
from convolearn.domain_maps import generate_domain_maps
from convolearn.adapter import adapt_dialogue
from tutor_eval.evaluation.analyzer import analyze_transcript

with open("convolearn/results/sampled_dialogues.json") as f:
    sampled_prompts = json.load(f)
with open("convolearn/results/domain_maps.json") as f:
    domain_maps = json.load(f)

client = anthropic.Anthropic()

# Score a single dialogue
entry = sampled_prompts[0]
domain_map = domain_maps[entry["prompt_id"]]
analysis_input = adapt_dialogue(
    prompt_id=entry["prompt_id"],
    question_prompt=entry["question_prompt"],
    dialogue=entry["dialogues"][0],
    domain_map=domain_map,
    bkt_preset="absent",
)
result = analyze_transcript(analysis_input, client, compute_nac=True)
print(result.composite, result.kft, result.pr)
```

The CLI always re-runs all five stages. If Stages 1 and 2 are expensive for your use case
(e.g., a very large sample), this Python API approach avoids re-running them.

---

## API Call Cost Estimate

Each dialogue requires:
- **1 Haiku call per student turn** — BKT observation classification
- **1 Haiku call per tutor turn** — NAC/KFT/LCQ/MRQ classifier

For a 10-exchange dialogue (`num_exchanges=10`), that is roughly 20 total calls.

| Configuration | Calls per dialogue | 7 prompts × 20 dialogues |
|---|---|---|
| `--nac --lcq` (full) | ~20 | ~2,800 Haiku calls |
| `--no-nac --no-lcq` | ~20 | ~2,800 Haiku calls |

Note: disabling NAC and LCQ does **not** reduce the call count — both are part of the same
per-tutor-turn prompt. The flags only affect which values appear in the output.

Domain map generation (Stage 2) costs **1 Sonnet call per prompt** with `--domain-source sentence`
(default), or **2 Sonnet calls** with `--domain-source article`. This is a one-time cost per mode;
maps are cached and re-used on all subsequent runs.

---

## File Structure

```
convolearn/
  __init__.py
  parse.py          # Stage 1: HuggingFace dataset loading & sampling
  domain_maps.py    # Stage 2: domain map generation (wraps domain_resolver)
  adapter.py        # Stage 3: ConvoLearn dialogue → analysis_input (wraps ingestion layer)
  score_batch.py    # Stages 4+5: batch scoring CLI entry point + result aggregation
  results/          # gitignored — output files from pipeline runs
    sampled_dialogues.json
    domain_maps.json
    scored_results.json
    summary.json
```

---

## Dependencies on Existing Code

| Component | Location | Used in |
|---|---|---|
| `analyze_transcript()` | `tutor_eval/evaluation/analyzer.py` | Stage 4 |
| `prepare_analysis_input()` | `tutor_eval/ingestion/converter.py` | Stage 3 |
| `resolve_domain_map()` | `tutor_eval/ingestion/domain_resolver.py` | Stage 2 |
| `compute_domain_map()` / `enrich_domain_map()` | `tutor_eval/tutors/socratic.py` | via Stage 2 |
| HuggingFace `datasets` | installed in `.venv` | Stage 1 |

**Patch applied to `converter.py`:** `"teacher"` → `"tutor"` role normalization was added
alongside the existing `"student"` → `"user"` mapping, so ConvoLearn's role labels are
handled transparently.
