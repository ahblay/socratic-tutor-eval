# Socratic Tutor Evaluation Framework

An automated pipeline for evaluating Socratic tutoring quality. The framework
replays conversation transcripts, tracks student knowledge state via Bayesian
Knowledge Tracing (BKT), and scores tutors on five metrics measuring compliance,
targeting, progression, scaffolding appropriateness, and misconception handling.

Validation is performed against the
[ConvoLearn](https://huggingface.co/datasets/masharma/convolearn) benchmark — a
dataset of human tutor–student Earth Science dialogues with ground-truth
pedagogical effectiveness ratings.

---

## Repository Layout

```
tutor_eval/
  evaluation/
    analyzer.py       # Post-hoc transcript analyzer (entry point: analyze_transcript())
    bkt.py            # Bayesian Knowledge Tracing evaluator
    metrics.py        # Pure-Python metric computation (NAC, KFT, PR, LCQ, MRQ)
  ingestion/
    converter.py      # Raw transcript → analysis_input format
    domain_resolver.py
  tutors/
    base.py           # AbstractTutor
    socratic.py       # SocraticTutor (full Socratic implementation)
    generic.py        # GenericTutor (bare / instructed baselines)
  student/
    convolearn_agent.py  # ConvoLearn Jamie student agent

convolearn/
  score_batch.py      # CLI: score ConvoLearn dialogues against ground truth
  simulate.py         # CLI: run multi-condition tutor comparison
  sim_conditions.py   # Condition registry and topic slugs
  adapter.py          # ConvoLearn dialogue → analysis_input
  results/            # Output files (gitignored)

docs/
  evaluation_plan.md  # Authoritative metric design document
  convolearn.md       # ConvoLearn pipeline deep-dive
  plans/
    multi_condition_simulation.md  # Simulation design and analysis plan

tests/
```

---

## Setup

**Requirements:** Python ≥ 3.12, an Anthropic API key.

```bash
# 1. Clone the repository
git clone https://github.com/ahblay/socratic-tutor-eval.git
cd socratic-tutor-eval

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install the package and all dependencies
pip install -e .

# 4. Set your Anthropic API key
# Add to ~/.zshrc (or ~/.bashrc):
export ANTHROPIC_API_KEY="sk-ant-..."
# Then reload:
source ~/.zshrc
```

**Verify setup:**
```bash
python -m convolearn.score_batch --parse-only --sample-size 3
```
This runs Stage 1 only (no API calls) and prints the sampled prompts. If you see
output without errors, the environment is correctly configured.

---

## Metrics

| Metric | What it measures | Computation |
|--------|-----------------|-------------|
| **NAC** | Non-Answer Compliance — did the tutor avoid giving direct answers? | LLM classifier per tutor turn; `compliant_turns / total_turns` |
| **KFT** | Knowledge Frontier Targeting — did the tutor address what the student needs next? | LLM identifies targeted KC; `on_frontier_turns / total_turns` |
| **PR** | Progression Rate — did BKT mastery probability increase consistently? | Stall detection (K=3 turns, δ=0.05); `1 − stall_fraction` |
| **LCQ** | Lesson Calibration Quality — was the tutor's response style appropriate for this student? | LLM judges observed vs. warranted response type per turn |
| **MRQ** | Misconception Response Quality — did the tutor probe student misconceptions Socratically? | `probed_turns / misconception_turns`; `null` when no misconceptions detected |

**Composite formula** (from `docs/evaluation_plan.md`):
```
composite = NAC × (0.5 × KFT + 0.25 × PR + 0.25 × LCQ + MRQ_adjustment)
MRQ_adjustment = 0.15 × (MRQ − 0.5)  if misconceptions present, else 0.0
```

NAC is a multiplicative wrapper — a tutor that gives direct answers collapses the
score regardless of how well it targets KCs or progresses the student.

**BKT parameters:** P_L0 (absent=0.10, partial=0.50, mastered=0.90),
P_TRANSIT=0.10, P_GUESS=0.25, P_SLIP=0.10. Mastery threshold: p ≥ 0.70.

---

## Experiment 1 — ConvoLearn Benchmark

Scores human ConvoLearn dialogues with `analyze_transcript()` and compares
metric values against `effectiveness_consensus` and `completeness_consensus`
ground-truth ratings. This validates whether the automated metrics correlate with
human judgement of teaching quality.

### Running

```bash
# Step 1 — Inspect the sampled prompts (no API calls)
python -m convolearn.score_batch \
  --parse-only \
  --sample-size 7 \
  --output-dir convolearn/results/

# Step 2 — Cheap validation: score 3 dialogues per prompt
python -m convolearn.score_batch \
  --from-sample \
  --max-dialogues-per-prompt 3 \
  --workers 2 \
  --output-dir convolearn/results/

# Step 3 — Full run (re-uses saved sample and domain maps)
python -m convolearn.score_batch \
  --from-sample \
  --workers 4 \
  --output-dir convolearn/results/

# Append more dialogues without re-scoring existing sessions
python -m convolearn.score_batch \
  --from-sample \
  --append \
  --output-dir convolearn/results/
```

### Key flags

| Flag | Default | Description |
|------|---------|-------------|
| `--sample-size N` | 7 | Number of distinct question prompts to sample |
| `--seed N` | 42 | RNG seed for reproducible prompt selection |
| `--workers N` | 4 | Parallel scoring threads |
| `--no-nac` / `--no-lcq` | — | Emit `null` for that metric (does not reduce API calls) |
| `--initial-knowledge` | `tabula_rasa` | BKT prior: `tabula_rasa`, `prereqs_mastered`, `all_partial` |
| `--domain-source` | `sentence` | `sentence`: compact 4–7 KC map (1 Sonnet call/prompt). `article`: full 12–20 KC map (2 calls/prompt) |
| `--parse-only` | off | Stage 1 only — sample dataset and exit. Zero API calls |
| `--from-sample` | off | Skip Stage 1+2 if output files already exist |
| `--append` | off | Add to existing `scored_results.json`; skip already-scored sessions |
| `--max-dialogues-per-prompt N` | no limit | Cap dialogues scored per prompt (for incremental runs) |

### Output files (`convolearn/results/`)

- **`sampled_dialogues.json`** — selected prompts and their raw dialogues
- **`domain_maps.json`** — generated KC maps per prompt (cached; never regenerated)
- **`scored_results.json`** — one record per dialogue with all metric scores and
  ground-truth ratings attached
- **`summary.json`** — per-prompt mean scores

### Results (153 dialogues, 4 prompts)

Pearson r between automated metrics and ConvoLearn ground-truth ratings,
aggregated across all prompts:

| Metric | vs Effectiveness r / p | vs Completeness r / p |
|--------|----------------------|-----------------------|
| NAC | **+0.335 / <0.001** | **+0.273 / 0.001** |
| KFT | +0.084 / 0.299 | +0.117 / 0.150 |
| PR | −0.016 / 0.845 | −0.029 / 0.724 |
| LCQ | +0.131 / 0.106 | +0.060 / 0.461 |
| MRQ | −0.109 / 0.490 (n=42) | −0.113 / 0.474 (n=42) |

NAC is the only metric with consistent, statistically significant correlation.
See `docs/convolearn.md` for per-prompt breakdown.

---

## Experiment 2 — Multi-Condition Simulation

Compares three tutor conditions on a single Earth Science topic using a synthetic
student agent (Jamie, a struggling 7th-grader). All three conditions share the
same domain map to isolate the effect of instruction style.

| Condition | Style | System prompt |
|-----------|-------|---------------|
| `socratic` | Full Socratic | `SocraticTutor` — never provides answers, guides by questioning |
| `instructed-map` | Guided | ~4 sentences of pedagogical guidance + domain map |
| `bare-map` | Minimal | "You are a helpful teacher" + domain map |

### Running

```bash
# Smoke test — verify pipeline before a full run (2 conditions, 1 rep)
python -m convolearn.simulate \
  --conditions socratic,bare-map \
  --turns 10 \
  --reps 1 \
  --prompt-id i-m-looking-at-a-topographic-map-with-a-contour-interval-of-5-meters-how-can-i-d \
  --output-dir convolearn/results/sim/

# Full pilot (3 conditions, 8 reps; use --workers 1 to avoid rate-limit errors)
python -m convolearn.simulate \
  --conditions socratic,instructed-map,bare-map \
  --turns 10 \
  --reps 8 \
  --prompt-id i-m-looking-at-a-topographic-map-with-a-contour-interval-of-5-meters-how-can-i-d \
  --output-dir convolearn/results/sim/ \
  --workers 1

# Append additional reps without re-running existing sessions
python -m convolearn.simulate \
  --conditions socratic,instructed-map,bare-map \
  --turns 10 \
  --reps 8 \
  --prompt-id i-m-looking-at-a-topographic-map-with-a-contour-interval-of-5-meters-how-can-i-d \
  --output-dir convolearn/results/sim/ \
  --workers 1 \
  --append
```

**`--prompt-id`** must match a key in `convolearn/results/domain_maps.json`. The
topographic map prompt above was selected for having the strongest evaluator–
ground-truth correlation.

**Rate limits:** Each session makes ~20 Haiku API calls. With `--workers > 1`,
concurrent calls may hit the 50k input-token/minute limit, causing 429 errors in
the response reviewer. These are non-fatal (the session continues with the
unreviewed response), but `--workers 1` eliminates them.

### Pre-flight checklist (run after smoke test, before full pilot)

1. `sim_results.json` contains `pilot_composite`, `nac_adjusted_composite`, and
   `analyzer_composite` as distinct fields
2. `nac_adjusted_composite` ≥ `pilot_composite` for every session (NAC ≤ 1.0)
3. `lcq` is non-null; `pilot_composite` formula does not include it
4. `GenericTutor.session_state() → None` does not crash `analyze_transcript()`
5. `sim_transcripts.json` turns use `"student"/"tutor"` roles (not `"user"/"tutor"`)
6. Spot-check 1 transcript per condition: verify Jamie expresses genuine confusion
   across all three tutors, not passive absorption after direct instruction

### Output files (`convolearn/results/sim/`)

- **`sim_results.json`** — one record per session with all metric scores and
  composite values
- **`sim_summary.json`** — per-condition means and standard deviations
- **`sim_transcripts.json`** — raw dialogue turns per session (for manual review
  or webapp import)

### Composite formulas

The simulation stores three composites per session:

```
pilot_composite        = NAC × (0.667 × KFT + 0.333 × PR + MRQ_adj)
nac_adjusted_composite = 1.0 × (0.667 × KFT + 0.333 × PR + MRQ_adj)
analyzer_composite     = NAC × (0.5 × KFT + 0.25 × PR + 0.25 × LCQ + MRQ_adj)
```

Use `nac_adjusted_composite` for comparing pedagogical effectiveness across
conditions: `pilot_composite` penalises non-Socratic tutors by design.

### Pilot results (8 reps per condition, Haiku 4.5)

| Condition | NAC | KFT | PR | LCQ | Full composite (μ ± σ) | NAC-Adj (μ ± σ) |
|-----------|-----|-----|----|-----|-----------------------|-----------------|
| bare-map | 0.200 | 0.525 | 0.975 | 0.288 | 0.114 ± 0.055 | 0.569 ± 0.097 |
| instructed-map | 0.350 | 0.700 | 0.938 | 0.475 | 0.259 ± 0.132 | 0.713 ± 0.102 |
| socratic | 0.963 | 0.875 | 0.988 | 0.938 | 0.885 ± 0.083 | 0.919 ± 0.050 |

Full composite uses the `analyzer_composite` formula (includes LCQ). NAC-Adj
pins NAC = 1.0 and includes LCQ. Both gaps (socratic vs instructed-map: 0.206;
instructed-map vs bare-map: 0.144) exceed the 0.10 decision threshold, indicating
the framework discriminates meaningfully across all three conditions.

### Decision table

| Outcome | Interpretation | Next step |
|---------|---------------|-----------|
| Gaps ≥ 0.10 on `nac_adjusted_composite` | Framework discriminates; Socratic adds measurable value | Proceed to multi-topic study |
| All gaps < 0.10, std < 0.07 | Inconclusive; framework is stable | Increase n to ≥ 20 per condition |
| All gaps < 0.10, std > 0.10 | High within-condition variance overwhelms signal | Audit classifier consistency before expanding |
| `bare-map` ≥ `socratic` on `nac_adjusted_composite` | KFT/PR not discriminating on this topic | Manually audit 2–3 transcripts per condition |

---

## Running Tests

```bash
source .venv/bin/activate
pytest
```

---

## Cost Estimates

**ConvoLearn benchmark** (~153 dialogues, 10 turns each):
~3,060 Haiku calls. At current Haiku pricing, approximately **$1–2** for a full run.
Domain map generation is a one-time cost: 1 Sonnet call per prompt (~4 calls total
with `--domain-source sentence`).

**Multi-condition simulation** (3 conditions × 8 reps × 10 turns):
- Student (Jamie, Haiku): ~$0.03/session
- Tutor: socratic (Haiku) ~$0.03/session; generic (Haiku) ~$0.03/session
- Evaluation (Haiku): ~$0.06/session
- **Total pilot run: ~$5**

---

## Known Limitations

- **KFT conflates two failure modes** for non-Socratic tutors: (1) broad responses
  that span multiple KCs simultaneously cause the KC classifier to return null
  (`off_map`); (2) early direct instruction floods BKT mastery, making later turns
  target already-mastered KCs (`mastered`). Both penalise KFT equally. KFT is a
  reliable signal for Socratic-style tutors but a mixed signal for direct-instruction
  conditions.
- **LCQ requires enriched domain maps.** Slim ConvoLearn maps (generated with
  `--domain-source sentence`) have no `knowledge_type` field, causing
  `warranted_type` to default to `"concept"` for every KC. LCQ is excluded from
  the simulation composite for this reason. It is valid when full domain maps are
  used (webapp sessions, or `--domain-source article`).
- **PR is near-ceiling at 10 turns.** Stall detection requires K=3 consecutive
  turns on the same KC; with 4–7 KCs and 10 turns, stalls rarely accumulate.
  PR ≈ 1.0 across all conditions is a valid null result, not a failure.
- **MRQ is sparse.** MRQ fires only when BKT detects a misconception in a student
  turn. With a synthetic student and no seeded misconceptions, most sessions
  return `mrq: null`. This is expected — null MRQ is not a scoring failure.
- **Single topic.** The pilot uses one Earth Science topic (topographic maps),
  selected for highest evaluator–ground-truth correlation. Results are directional,
  not generalisable.
- **Student confound.** Jamie (the synthetic student) absorbs direct instruction
  from bare/instructed tutors and responds more correctly in subsequent turns,
  which improves BKT state independent of tutor quality. The smoke-test
  spot-check (pre-flight item 6) is the primary mitigation.

See `KNOWN_ISSUES.md` for documented bugs and failure modes in the webapp layer.

---

## Further Reading

| Document | Contents |
|----------|----------|
| `docs/evaluation_plan.md` | Authoritative metric definitions, composite formula, BKT initialization |
| `docs/convolearn.md` | Full ConvoLearn pipeline reference, all CLI flags, output schemas |
| `docs/plans/multi_condition_simulation.md` | Simulation design, analysis plan, decision table, cost breakdown |
| `API.md` | curl-based reference for webapp admin workflows (session import, user management) |
