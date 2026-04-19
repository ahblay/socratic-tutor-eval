# ConvoLearn Evaluation Pipeline — Implementation Plan

## Goal

Validate the Socratic tutor evaluation framework against an external benchmark.
Run the existing evaluator (`analyze_transcript`) on a sample of real human
tutor–student dialogues from ConvoLearn, then compare the resulting per-metric
scores against ConvoLearn's own `effectiveness_consensus` and
`completeness_consensus` ratings. All raw metrics are emitted independently so
that the analysis phase (separate plan) can calibrate composite weights against
the ground-truth ratings.

---

## Dataset Overview

**Source**: HuggingFace — `masharma/convolearn`
**Format**: Parquet (load via HuggingFace `datasets` library), single `train`
split, 2,134 rows.

Each row is one tutor–student dialogue with the following fields:

| Field | Type | Description |
|-------|------|-------------|
| `earthscience_topic` | string (4 values) | Earth Science topic grouping |
| `kb_dim` | string (6 values) | Broader knowledge-building dimension |
| `kb_subdim` | string (21 values) | Specific knowledge-building subdimension |
| `effectiveness_consensus` | float64 | Pedagogical effectiveness rating (1–5) |
| `completeness_consensus` | float64 | Conversation completeness rating (1–3) |
| `cleaned_conversation` | string | Full dialogue text (see format below) |
| `num_exchanges` | int64 | Number of back-and-forth exchange pairs (2–13) |

**Conversation format** — plain text within `cleaned_conversation`:
```
Student: [opening question]
Teacher: [response]
Student: [follow-up]
...
```

Role labels are `Teacher:` / `Student:` (note capitalisation).

**Question prompt**: The student's opening message (first `Student:` line) is the
natural grouping key — dialogues sharing the same opening question are directly
comparable. This is more precise than `earthscience_topic`, which groups only 4
broad categories and conflates multiple distinct questions.

Target sample: **5–10 unique question prompts**, each with **≥ 20 dialogues**
of **≥ 10 exchange pairs** (`num_exchanges >= 10`).

---

## Pipeline Stages

### Stage 1 — Parse & Sample (`convolearn/parse.py`)

1. Load the dataset: `load_dataset("masharma/convolearn", split="train")`.
2. For each row, extract the **first Student utterance** from `cleaned_conversation`
   as the `question_prompt`. This is the grouping key.
3. Group rows by `question_prompt`.
4. Filter to prompts with ≥ `--min-dialogues` rows each having
   `num_exchanges >= --min-exchanges`.
5. Randomly sample up to `--sample-size` prompts (seed-controlled).
6. For each selected prompt, collect all qualifying dialogue rows.
7. Output: `sampled_dialogues.json` — list of objects:
   ```json
   {
     "prompt_id": "<slug of question_prompt>",
     "question_prompt": "...",
     "earthscience_topic": "...",
     "dialogues": [
       {
         "dialogue_idx": 0,
         "cleaned_conversation": "...",
         "effectiveness_consensus": 3.5,
         "completeness_consensus": 2.5,
         "num_exchanges": 11
       },
       ...
     ]
   }
   ```

### Stage 2 — Domain Map Generation (`convolearn/domain_maps.py`)

For each unique `question_prompt` in the sample:

1. Use the **question prompt text** (first Student message) as the topic source
   for domain map generation. Do **not** use `earthscience_topic` or `kb_dim` —
   those are pedagogical categories, not content descriptions.
2. Call `resolve_domain_map({"topic": question_prompt}, client, cache_dir)` from
   `tutor_eval/ingestion/domain_resolver.py`. Caching is handled automatically;
   re-running the pipeline on the same prompts costs nothing after the first run.
3. Output: `domain_maps.json` — `{ prompt_id: domain_map_dict }`.

**Configurable:** `--domain-source {sentence | article}` flag. `sentence`
(default) calls `resolve_domain_map` with the topic string. `article` expands
the question into a short article via LLM before generating the domain map
(Option B from the original plan; only needed if sentence-based maps are sparse).

### Stage 3 — Transcript Adaptation (`convolearn/adapter.py`)

Convert each ConvoLearn dialogue into the `analysis_input` format that
`analyze_transcript()` expects. **Reuse the existing ingestion layer** —
`adapter.py` is a thin wrapper over `tutor_eval/ingestion/`.

**Role normalization**: `Teacher` → `tutor`, `Student` → `student`.
`converter.py` then normalises `student` → `user` in `lesson_turns`.

**Step-by-step**:
1. Parse `cleaned_conversation` into a list of `{role, content}` turns using the
   `Teacher:` / `Student:` line prefixes. A turn begins on a new line starting
   with a role label and continues until the next role label (handles multi-line
   teacher responses).
2. Build a raw transcript dict:
   ```python
   {
     "session_id": f"{prompt_id}_{dialogue_idx}",
     "topic": question_prompt,         # used as article_id slug + domain map key
     "turns": [{"role": "tutor"|"student", "content": "..."}, ...],
     "bkt_preset": initial_knowledge,  # from --initial-knowledge flag
   }
   ```
3. Call `prepare_analysis_input(raw, domain_map)` from
   `tutor_eval/ingestion/converter.py` to produce the final `analysis_input`.

**Actual `analysis_input` shape** (what `analyze_transcript()` consumes):
```json
{
  "session_id": "<prompt_id>_<dialogue_idx>",
  "article_id": "<slug of question_prompt>",
  "article_title": "<question_prompt text>",
  "domain_map": { ... },
  "bkt_initial_states": { "<kc_id>": {"p_mastered": 0.1, ...}, ... },
  "assessment_turns": [],
  "lesson_turns": [
    {
      "turn_number": 1,
      "role": "user",
      "content": "...",
      "reviewer_verdict": null,
      "tutor_state_snapshot": null,
      "evaluator_snapshot": null
    },
    ...
  ]
}
```

**ConvoLearn metadata** (ground-truth ratings) is stored alongside the result,
not inside `analysis_input`. It is attached at Stage 4.

**BKT bootstrapping**: Controlled by `--initial-knowledge` flag, which maps to
`bkt_preset` in the raw transcript dict. Default: `tabula_rasa` → `"absent"`
(all KCs at p=0.10). No pre-session assessment data is available.

### Stage 4 — Batch Scoring (`convolearn/score_batch.py`)

1. For each adapted transcript, call `analyze_transcript(analysis_input, client,
   compute_nac=compute_nac_flag, compute_lcq=compute_lcq_flag)`.
2. Collect all raw metric values from `EvaluationResult`.
3. Attach ConvoLearn ground-truth ratings to produce one flat record per dialogue.
4. Output: `scored_results.json` — one record per dialogue:
   ```json
   {
     "session_id": "...",
     "prompt_id": "...",
     "earthscience_topic": "...",
     "nac":   0.85,
     "kft":   0.62,
     "pr":    0.90,
     "lcq":   0.70,
     "mrq":   null,
     "composite": 0.72,
     "is_valid": true,
     "total_tutor_turns": 11,
     "effectiveness_consensus": 4.0,
     "completeness_consensus": 2.5,
     "num_exchanges": 11
   }
   ```

**Disabled metrics emit `null`, not a default value.** `nac: null` when
`--no-nac`; `lcq: null` when `--no-lcq`. This allows the analysis phase to
distinguish "not computed" from "measured as zero/perfect", which matters for
calibration.

**Composite** is still computed using whatever metrics were enabled — if NAC is
disabled, the composite uses `nac=1.0` internally as before (the composite
field reflects the run configuration, while the individual `nac` field reflects
whether it was actually measured).

**Parallelisation**: Use `ThreadPoolExecutor` — `analyze_transcript()` is
synchronous and uses the blocking `anthropic.Anthropic()` client; `asyncio.gather`
is not compatible without a full async refactor.

### Stage 5 — Results Storage (`convolearn/results/`)

```
convolearn/results/
  sampled_dialogues.json      # Stage 1 output
  domain_maps.json            # Stage 2 output
  scored_results.json         # Stage 4 output (one flat record per dialogue)
  summary.json                # per-prompt aggregate stats
```

`summary.json` contains per-prompt means for each raw metric alongside mean
ConvoLearn ratings — ready for correlation analysis in a later phase:
```json
{
  "prompt_id": "...",
  "question_prompt": "...",
  "n_dialogues": 23,
  "mean_nac": 0.72,
  "mean_kft": 0.58,
  "mean_pr": 0.81,
  "mean_lcq": 0.65,
  "mean_mrq": 0.60,
  "mean_composite": 0.63,
  "mean_effectiveness_consensus": 3.4,
  "mean_completeness_consensus": 2.3
}
```

---

## Key Configuration Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--sample-size N` | 7 | Max number of unique question prompts to sample |
| `--min-dialogues N` | 20 | Min dialogues per prompt |
| `--min-exchanges N` | 10 | Min `num_exchanges` per dialogue |
| `--seed N` | 42 | RNG seed for reproducible sampling |
| `--nac / --no-nac` | `--nac` | Enable/disable NAC scoring; disabled → `nac: null` in output |
| `--lcq / --no-lcq` | `--lcq` | Enable/disable LCQ scoring; disabled → `lcq: null` in output |
| `--initial-knowledge` | `tabula_rasa` | BKT prior (`tabula_rasa` → `absent`, `partial`, `mastered`) |
| `--domain-source` | `sentence` | `sentence` = topic string; `article` = LLM-expanded article |
| `--workers N` | 4 | ThreadPoolExecutor worker count |
| `--output-dir PATH` | `convolearn/results/` | Output directory |

---

## File Structure

```
convolearn/
  __init__.py
  parse.py          # Stage 1: dataset loading & sampling
  domain_maps.py    # Stage 2: domain map generation & caching (wraps domain_resolver)
  adapter.py        # Stage 3: ConvoLearn → analysis_input (wraps ingestion layer)
  score_batch.py    # Stage 4 + 5: batch scoring CLI entrypoint
  results/          # Stage 5: output files (gitignored)
```

Run the full pipeline:
```bash
python -m convolearn.score_batch \
  --dataset masharma/convolearn \
  --sample-size 7 \
  --no-nac \
  --no-lcq \
  --output-dir convolearn/results/
```

---

## Resolved Questions

1. **Dataset format**: HuggingFace `masharma/convolearn`, Parquet, 2,134 rows,
   single `train` split. Field names confirmed above.
2. **Domain map from sentence**: `resolve_domain_map()` in
   `tutor_eval/ingestion/domain_resolver.py` handles this directly. Use
   `question_prompt` (first Student message) as the topic string, not the
   `earthscience_topic` or `kb_dim` categorical fields.
3. **BKT without assessment**: Tabula rasa is appropriate. No assessment data
   exists for ConvoLearn sessions; `bkt_preset="absent"` is the correct default.

## Open Questions

4. **Score comparison methodology**: Pearson/Spearman correlation, ranking
   agreement (Kendall τ), or Bradley-Terry pairwise — deferred to the
   correlation analysis plan.
5. **LLM cost estimate**: With `--nac --lcq` enabled, each dialogue requires
   ~10–20 Haiku calls; 20 dialogues × 7 prompts = ~1,400–2,800 calls. With
   `--no-nac --no-lcq`, BKT observation classification is the only LLM cost
   (~same call count, cheaper prompts).

---

## Dependencies on Existing Code

| Component | Source |
|-----------|--------|
| `analyze_transcript()` | `tutor_eval/evaluation/analyzer.py` |
| `prepare_analysis_input()` | `tutor_eval/ingestion/converter.py` |
| `resolve_domain_map()` | `tutor_eval/ingestion/domain_resolver.py` |
| `normalize_domain_map()` | `tutor_eval/ingestion/domain_resolver.py` |
| `BKTEvaluator` | `tutor_eval/evaluation/bkt.py` |
| `compute_domain_map()` | `tutor_eval/tutors/socratic.py` |

### Required patch — role normalisation in `converter.py`

`tutor_eval/ingestion/converter.py` currently normalises `"student"` → `"user"`
but does not handle `"teacher"`. Before Stage 3 runs, add:

```python
if role == "teacher":
    role = "tutor"
```

in `prepare_analysis_input()` alongside the existing `student` → `user` mapping.
