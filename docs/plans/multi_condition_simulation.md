# Multi-Condition Tutor Comparison: Implementation Plan

**Goal:** Determine whether the Socratic teaching approach outperforms generic LLM tutoring
on a single Earth Science topic. Results are directional — this is a **pilot to identify
which comparisons warrant a larger funded study**, not a statistically conclusive experiment.

**Input data:** Pre-generated domain maps in `convolearn/results/domain_maps.json` (4 Earth
Science topics). No re-generation needed.

**Topic:** `i-m-looking-at-a-topographic-map-with-a-contour-interval-of-5-meters-how-can-i-d`
(topic 3 — highest correlation between our evaluator and ConvoLearn's ground-truth ratings).

---

## Experimental Conditions

Three conditions, all using the same domain map. The domain map is held constant to isolate
the effect of tutor instruction level. This ensures metric comparability across all three
conditions: KFT, PR, NAC, and MRQ can all be computed using the same domain map.

| Condition ID | Style | Domain Map | Rationale |
|---|---|---|---|
| `socratic` | Full Socratic | Yes | Existing `SocraticTutor`; primary subject |
| `instructed-map` | Pedagogical guidance | Yes | Isolates value of strict Socratic rigor |
| `bare-map` | Minimal | Yes | Baseline — map provided, no instruction |

This trio answers two clean questions, with domain map held constant in both:

1. Does pedagogical instruction add value over a bare tutor? (`bare-map` vs `instructed-map`)
2. Does strict Socratic rigor add value over guidance? (`instructed-map` vs `socratic`)

The previously proposed `bare-nomap` condition is dropped. Without a domain map, KFT is
undefined (the classifier cannot identify KC targets → all turns score as `off_map`), making
comparison across conditions invalid. A no-map baseline is deferred to a study that includes
metrics not dependent on KC structure.

**Prompting levels for generic tutor:**

- `bare`: "You are a helpful teacher. Help the student understand: {topic}."
- `instructed`: ~4 sentences of pedagogical guidance — probe prior knowledge, guide with
  questions, use concrete examples, confirm understanding before moving on. No explicit
  "never give answers" rule.

When a domain map is provided to a generic tutor, the full map JSON is appended to the
system prompt after the base instruction.

---

## Simulation Design

**Student agent:** The Jamie persona from the ConvoLearn paper. No knowledge document —
Jamie is a generic struggling 7th-grader with no explicit knowledge state.

```
You are Jamie, a 7th grade student (age 12–13) who genuinely doesn't understand a specific
Earth Science concept. Your goal is to learn, not to test the teacher.
Core Identity:
• Respond with the vocabulary and sentence structure of a typical middle schooler.
• Show real confusion about the concept you're struggling with.
• Display the attention span and focus patterns of your age group.
• React naturally to explanations (sometimes getting it, sometimes still confused).
Communication Style:
• Keep responses short (typically 1–2 sentences).
• Use casual, age-appropriate language (e.g., "Wait, so...", "I'm still confused about...", "Oh,
  that makes sense!").
• Show when you're following along vs. when you're lost.
• Express frustration or excitement as a real student would.
Learning Behavior:
• Ask clarifying questions only when genuinely confused about what the teacher just said.
• Build on previous explanations rather than jumping to new topics.
• Sometimes misunderstand or partially understand concepts.
• Need concrete examples to grasp abstract ideas.
• May relate new concepts to things from your everyday experience.
What NOT to do:
• Don't ask leading questions or fish for specific information.
• Don't use technical terms correctly unless the teacher taught them to you first.
• Don't try to guide the lesson or suggest what to cover next.
• Don't demonstrate knowledge beyond what a struggling student would have.
Your current struggle: {question_prompt}
Reminder: You're here to learn, not teach. Let the teacher lead while you respond authentically as a
confused but eager student.
```

The original ConvoLearn prompt uses JavaScript template variables. In `ConvoLearnStudentAgent`,
substitute `{question_prompt}` for `${currentQuestion.question}`. The `${currentQuestion.dimension}`
field ("Teaching focus") is omitted — it was metadata for the ConvoLearn teacher, not a student-visible
field, and provides no signal for a synthetic student agent.

Anthropic Haiku is used for the student model (cheap, fast).

**Session parameters:**
- Turns per session: 10 (above the 8-turn validity floor; reduces cost ~33% vs. 15 turns)
- Reps per condition: 8 (sufficient to estimate within-condition variance)
- Total sessions: 1 topic × 3 conditions × 8 reps = **24 sessions**

**Evaluation:** `analyze_transcript()` with `tabula_rasa` BKT preset and NAC enabled.
BKT initial states derived from domain map (absent preset: all KCs at p=0.10).

**Note on slim domain maps:** The ConvoLearn maps were generated with `--domain-source sentence`
(4–7 KCs, no enrichment). They lack `knowledge_type` and `reference_material` fields.
`SocraticTutor` handles this gracefully (falls back to `concept` type). This affects
LCQ — see below. KFT and PR are still meaningful since BKT runs over these KCs.

---

## Metrics

### Active metrics

| Metric | Status | Notes |
|---|---|---|
| **NAC** | Primary | Always measurable. Expected to rank: `socratic` >> `instructed-map` > `bare-map`. |
| **KFT** | Primary | Domain map available for all conditions → KC targeting is meaningful. |
| **MRQ** | Include, sparse | Fires only when BKT detects student misconceptions. Expected null for many sessions. |
| **PR** | Diagnostic only | Structurally limited at 10 turns — see below. |
| **LCQ** | Excluded from composite | Structurally invalid with slim maps — see below. |
| **TBA** | Unavailable | `GenericTutor.session_state()` returns `None`. Excluded automatically. |

### LCQ — why excluded from composite (structural, not noise)

LCQ requires two independent signals per tutor turn:
- `observed_type` — what the tutor actually did (concept / convention / narrative)
- `warranted_type` — what was appropriate given the KC and student BKT state

`warranted_type` is derived from the KC's `knowledge_type` field in the domain map. Slim
ConvoLearn maps have no `knowledge_type` field, so `_build_kc_type_map()` defaults every KC
to `"concept"`. The classifier prompt's warranted-type rules then always produce `"concept"`
regardless of the student's BKT state or which KC is targeted — because the conditions that
produce `"convention"` or `"narrative"` warranted types are unreachable.

With `warranted_type` locked to `"concept"`, LCQ degrades to: *"what fraction of turns did
the tutor use pure Socratic questioning?"* — which is redundant with NAC and does not capture
whether the tutor scaffolded appropriately for this student.

**LCQ is still computed by the classifier** (it shares a Haiku call with NAC/KFT/MRQ) and
values are recorded in `sim_results.json` for reference. It is **not used in the composite
formula for this experiment.** The simulation computes its own composite directly (see below)
rather than relying on `analyze_transcript()` to re-normalize.

### PR — limited at 10 turns

PR stall detection requires K=3 consecutive turns on the same KC with no BKT progress.
With 4–7 KCs and 10 tutor turns, expected turns per KC is ~1.5–2.5. A well-behaved
tutor will switch KCs naturally, making stalls unlikely to register. PR ≈ 1.0 across
all conditions is a plausible null result, not a failure.

`bare-map` may stall more than `socratic` (no instruction to move on), so PR *could*
discriminate — but this is speculative. **Treat PR as diagnostic.** If PR is uniformly
high across all conditions, it provides no signal at this session length.

### MRQ — how it works

MRQ runs as a two-step pipeline across adjacent turns:

1. **Student turn:** `classify_observations()` classifies each KC observation as
   `strong_articulation`, `partial_articulation`, `absent`, or **`misconception`**. Any
   `misconception` observations are stored in `pending_observations`.
2. **Next tutor turn:** the classifier receives a `MISCONCEPTION DETECTED` section and
   classifies whether the tutor probed the misconception Socratically (`"probed"`) or
   ignored/corrected it directly (`"ignored"`).

```
MRQ = probed_turns / total_misconception_turns
```

Returns `None` when no misconceptions arise in a session. With Jamie as a 7th-grader on
topographic maps and no seeded misconceptions, MRQ will likely be `None` for many sessions.
Where it fires, it is meaningful and worth reporting — misconception handling is a key
behavioral differentiator across tutor styles. **Do not treat null MRQ as a failure.**

---

## Composite Formula

`analyze_transcript()` computes its own composite using the full formula including LCQ.
`simulate.py` computes a separate **pilot composite** that excludes LCQ:

```
pilot_composite = NAC × (0.667 × KFT + 0.333 × PR + MRQ_adjustment)
```

KFT retains its 2:1 weight advantage over PR. `simulate.py` also records
`nac_adjusted_composite` (NAC pinned to 1.0) for cross-condition pedagogical comparison:

```
nac_adjusted_composite = 1.0 × (0.667 × KFT + 0.333 × PR + MRQ_adjustment)
```

Both are computed in `simulate.py` from raw metric values. The `analyze_transcript()`
composite (which includes LCQ at 0.25 weight) is stored separately as `analyzer_composite`
for auditing, but is not the primary metric for this experiment.

---

## NAC Handling for Non-Socratic Conditions

NAC is a multiplicative wrapper. `bare-map` and `instructed-map` are not designed to
withhold answers, so they will naturally score NAC ≈ 0.2–0.5. Multiplying by this value
collapses the pilot composite before KFT/PR can contribute signal.

`pilot_composite` answers: "how does each tutor score on the Socratic rubric as designed?"
`nac_adjusted_composite` answers: "how does each tutor perform on pedagogical effectiveness
(KFT/PR) independent of Socratic compliance?"

Both are reported. The analysis section below specifies which to use for each question.

---

## New Files

### `tutor_eval/tutors/generic.py`

```python
class GenericTutor(AbstractTutor):
    def __init__(
        self,
        topic: str,
        domain_map: dict | None = None,
        prompt_level: str = "bare",   # "bare" | "instructed"
        model: str = "claude-haiku-4-5-20251001",
        api_base: str | None = None,
        api_key: str | None = None,
    ): ...
```

- Uses `openai.OpenAI(base_url=api_base, api_key=api_key)` for all providers.
  This is an intentional simplicity tradeoff for the pilot — Anthropic's OpenAI-compatible
  endpoint supports Haiku/Sonnet without tool use or streaming, which is all this pilot needs.
- Maintains rolling history (last 12 turns, alternating user/assistant).
- `session_state()` returns `None` — TBA metric is skipped automatically.
- No response reviewer, no accuracy review, no state updates.
- Default model is Haiku (not GPT-4o) — cross-model comparison is out of scope.

**Provider endpoints:**

| Model | `api_base` | API key env var |
|---|---|---|
| `claude-haiku-4-5-20251001` | `https://api.anthropic.com/v1` | `ANTHROPIC_API_KEY` |
| `claude-sonnet-4-6` | `https://api.anthropic.com/v1` | `ANTHROPIC_API_KEY` |

An explicit `--api-base` flag overrides for custom endpoints.

### `tutor_eval/student/convolearn_agent.py`

```python
class ConvoLearnStudentAgent:
    def __init__(self, question_prompt: str): ...
    def generate_message(self, last_tutor: str, history: list[dict]) -> dict: ...
```

- Verbatim Jamie system prompt with `question_prompt` substituted.
- Anthropic Haiku, max 256 tokens (student responses are brief).
- Returns `{"message": str}` — no self-assessment block needed.
- Maintains its own rolling history (last 8 turns) independently of the tutor.

### `convolearn/simulate.py`

Multi-condition simulation CLI. Stages:

1. **Load** domain maps + topic list from `sampled_dialogues.json`.
2. **Build work list:** (topic, condition, rep) tuples. Skip already-scored
   session IDs if `--append` is set.
3. **Run dialogues** in parallel (ThreadPoolExecutor, default 4 workers).
   Each dialogue: `ConvoLearnStudentAgent` ↔ `GenericTutor` or `SocraticTutor`.
4. **Track raw turns** as `[{"role": "student"|"tutor", "content": str}]` before
   normalization. These are stored separately from `analysis_input` and are required
   for the webapp import step (see Import section below).
5. **Convert** raw turns → `analysis_input` via `prepare_analysis_input()`.
6. **Score** with `analyze_transcript()`.
7. **Compute pilot metrics** (`pilot_composite`, `nac_adjusted_composite`) from raw scores.
8. **Write** `sim_results.json`, `sim_summary.json`, and `sim_transcripts.json`.

```bash
# Smoke test (1 topic, 2 conditions, 1 rep each)
python -m convolearn.simulate \
  --conditions socratic,bare-map \
  --turns 10 \
  --reps 1 \
  --prompt-id i-m-looking-at-a-topographic-map-with-a-contour-interval-of-5-meters-how-can-i-d \
  --output-dir convolearn/results/sim/

# Pilot run (3 conditions, 8 reps)
python -m convolearn.simulate \
  --conditions socratic,instructed-map,bare-map \
  --turns 10 \
  --reps 8 \
  --prompt-id i-m-looking-at-a-topographic-map-with-a-contour-interval-of-5-meters-how-can-i-d \
  --output-dir convolearn/results/sim/
```

**Session ID schema:** `{topic_slug}_{condition}_{model_short}_{rep}` where `topic_slug` is
a manually defined short alias (e.g. `topo-map`) declared as a constant in `sim_conditions.py`.
The full `prompt_id` is stored in `sim_results.json` for reference. Keeping session IDs short
is required for `--append` deduplication and webapp import legibility.

Example: `topo-map_socratic_sonnet_0`, `topo-map_bare-map_haiku_3`.

### `convolearn/sim_conditions.py`

Condition registry. Maps condition ID → `(prompt_level, include_domain_map)`.
Also defines the short topic slug alias. Keeps `simulate.py` clean — adding a condition
or topic is one dict entry.

```python
CONDITIONS = {
    "socratic":       ("socratic", True),
    "instructed-map": ("instructed", True),
    "bare-map":       ("bare", True),
}

TOPIC_SLUGS = {
    "i-m-looking-at-a-topographic-map-with-a-contour-interval-of-5-meters-how-can-i-d": "topo-map",
}
```

---

## Output Schema

### `sim_results.json`

One record per session. `lcq` is recorded but excluded from composite computation.
`analyzer_composite` is the raw value from `analyze_transcript()` (includes LCQ at 0.25
weight) — stored for auditing only.

```json
{
  "session_id": "topo-map_socratic_sonnet_0",
  "prompt_id": "i-m-looking-at-a-topographic-map...",
  "condition": "socratic",
  "model": "claude-sonnet-4-6",
  "rep": 0,
  "nac": 0.92,
  "kft": 0.58,
  "pr": 0.85,
  "lcq": 0.74,
  "mrq": null,
  "mrq_adjustment": 0.0,
  "pilot_composite": 0.73,
  "nac_adjusted_composite": 0.79,
  "analyzer_composite": 0.71,
  "is_valid": true,
  "total_tutor_turns": 10,
  "error": null
}
```

`mrq: null` means no misconceptions were detected — this is expected and not a failure.
`lcq` is always non-null (the classifier always runs) but is not used in `pilot_composite`.

### `sim_summary.json`

Per-condition aggregation across reps. Primary comparison columns are
`mean_pilot_composite` and `mean_nac_adjusted_composite`.

```json
[
  {
    "condition": "socratic",
    "model": "claude-sonnet-4-6",
    "n_sessions": 8,
    "mean_nac": 0.91,
    "mean_kft": 0.55,
    "mean_pr": 0.82,
    "mean_lcq": 0.71,
    "mean_mrq": null,
    "mean_pilot_composite": 0.70,
    "std_pilot_composite": 0.09,
    "mean_nac_adjusted_composite": 0.76,
    "std_nac_adjusted_composite": 0.08,
    "mean_analyzer_composite": 0.68
  }
]
```

### `sim_transcripts.json`

Raw dialogue turns per session, in the format required for webapp import (see Import section).
One entry per session:

```json
[
  {
    "session_id": "topo-map_socratic_sonnet_0",
    "topic": "I'm looking at a topographic map...",
    "domain_map": { ... },
    "turns": [
      {"role": "student", "content": "I'm not sure how to read these lines..."},
      {"role": "tutor",   "content": "What do you notice about where the lines..."}
    ]
  }
]
```

Note: `turns` uses `"student"/"tutor"` (not `"user"/"tutor"`). This matches the format
expected by `POST /api/import/sessions`. The role normalization to `"user"` happens inside
`prepare_analysis_input()` for scoring only — the raw turns must preserve original roles.

---

## Analysis Plan

The goal is directional signal, not statistical proof. With n=8 per condition, a 95% CI
on the mean pilot composite is roughly ±0.07 (assuming std ≈ 0.10). Differences smaller
than ~0.10 between conditions are inconclusive at this sample size.

**Questions and which metric to use:**

| Question | Metric |
|---|---|
| Does our rubric rank conditions as expected? | `pilot_composite` — `socratic` should rank highest |
| Does Socratic behavior improve pedagogical targeting? | `nac_adjusted_composite`: `socratic` vs `instructed-map` |
| Does instruction add value over bare baseline? | `nac_adjusted_composite`: `instructed-map` vs `bare-map` |
| Is NAC a meaningful differentiator? | `mean_nac` per condition — should strongly rank `socratic` > `instructed-map` > `bare-map` |
| Is PR informative at this session length? | `mean_pr` per condition — if ≈ 1.0 everywhere, discard |
| Is the framework stable across reps? | `std_pilot_composite` per condition — low variance = reliable |

**Decision table — acting on results:**

| Outcome | Interpretation | Next step |
|---|---|---|
| `socratic` `nac_adjusted_composite` > `instructed-map` by ≥ 0.10 | Socratic system adds measurable value | Proceed to multi-topic study |
| All gaps < 0.10 but `std` < 0.07 | Pilot inconclusive; framework is stable | Increase n to ≥ 20 per condition |
| All gaps < 0.10 and `std` > 0.10 | High within-condition variance overwhelms signal | Investigate classifier consistency before expanding |
| `bare-map` ≥ `socratic` on `nac_adjusted_composite` | Socratic instruction adds no value on this topic, or KFT/PR are not discriminating | Audit 2–3 transcripts per condition manually; check if Jamie responds differently across tutor styles |
| `std_pilot_composite` for `socratic` > 0.15 | Socratic tutor is inconsistent across runs | Investigate temperature/seed sensitivity before expanding |

Report per-condition mean ± std for `pilot_composite` and `nac_adjusted_composite`.

---

## Webapp Import and Visualization

After the pilot run, individual sessions can be loaded into the analysis viewer at
`/static/analysis.html?session_id=<id>`. The viewer shows:

- KC graph with BKT coloring per turn (driven by `bkt_snapshot` in `TurnResult`, not `tutor_state_snapshot` — so all three conditions display correctly, including `bare-map` which has no tutor state)
- Frame-by-frame slider: one frame per tutor turn
- Per-turn: targeted KC, NAC verdict, stall flag, MRQ verdict, BKT observations, full dialogue

**Import step** (requires webapp running and superuser token):

```bash
# For each session in sim_transcripts.json:
POST /api/import/sessions
{
  "transcript": {
    "session_id": "topo-map_socratic_sonnet_0",
    "topic": "I'm looking at a topographic map...",
    "domain_map": { ... },
    "turns": [{"role": "student"|"tutor", "content": "..."}]
  },
  "result": { /* EvaluationResult.to_dict() from sim_results */ }
}
```

**Critical format constraint:** `transcript.turns` must use `"student"/"tutor"` roles (not
`"user"/"tutor"`). `import_session` validates `role in ("student", "tutor")` and returns
422 otherwise. This is why `sim_transcripts.json` preserves raw roles from the dialogue loop,
before `prepare_analysis_input()` normalizes them.

**Turn number alignment:** Both `import_session` (DB rows) and `prepare_analysis_input()`
(analysis_input) assign turn numbers 1..N sequentially from the same ordered list. The
`analysis-view` endpoint's student-message lookup (`t.turn_number < tn`) therefore resolves
correctly without any offset adjustment.

An import utility script (`convolearn/import_to_webapp.py`) should be included as a
deliverable alongside `simulate.py`. It reads `sim_transcripts.json` and `sim_results.json`
and posts each session to the import endpoint. Sessions already imported return 409 and are
skipped.

---

## Changes to Existing Files

| File | Change |
|---|---|
| `pyproject.toml` | Add `openai>=2.0` to `dependencies` (already installed in venv) |
| `convolearn/score_batch.py` | None |
| `tutor_eval/tutors/socratic.py` | None — already accepts `domain_map: dict` |
| `tutor_eval/ingestion/converter.py` | None |
| `tutor_eval/evaluation/analyzer.py` | None — pilot composite computed in `simulate.py` directly |

---

## Pre-flight Verification

Before the full pilot run, verify the following during the smoke test:

1. `sim_results.json` contains `pilot_composite`, `nac_adjusted_composite`, and `analyzer_composite` as distinct fields
2. `nac_adjusted_composite` is always ≥ `pilot_composite` (NAC ≤ 1.0 always)
3. `lcq` is non-null (classifier ran) but `pilot_composite` formula does not include it
4. `session_state() → None` for GenericTutor does not crash `analyze_transcript()`
5. `sim_transcripts.json` turns use `"student"/"tutor"` roles, not `"user"/"tutor"`
6. **Transcript spot-check:** manually read 1 transcript from each condition and verify Jamie is simulating struggle across all three — not absorbing information passively when the tutor explains directly. This is the primary student-confound check. If Jamie in `bare-map` sessions stops expressing confusion after the tutor provides an explanation, KFT scores in subsequent turns may be inflated by Jamie's improved responses, not by tutor quality.

---

## Implementation Order

1. **`tutor_eval/tutors/generic.py`** — core building block; testable in isolation
2. **`tutor_eval/student/convolearn_agent.py`** — quick, nearly standalone
3. **`convolearn/sim_conditions.py`** — condition registry + topic slug map (no logic)
4. **`convolearn/simulate.py`** — wire everything together; track raw turns separately
5. **Smoke test** on topic 3 × `socratic` + `bare-map` × 1 rep each:
   - Verify `sim_results.json` shape and all composite fields present
   - Verify `sim_transcripts.json` role format
   - Run pre-flight verification checks 1–6 above
   - Spot-check transcripts from both conditions manually
6. **`convolearn/import_to_webapp.py`** — import utility; verify one session loads in viewer
7. **Pilot run** (3 conditions × 8 reps)

---

## Cost Estimate (Pilot Run)

Per session (10 turns):
- Student (Jamie, Haiku): 10 calls × ~200 tokens → ~$0.03
- Tutor: `socratic` (Sonnet) ~$0.30; `instructed-map`/`bare-map` (Haiku) ~$0.03
- Evaluation (Haiku): 20 calls × ~300 tokens → ~$0.06
- **Per session (`socratic`):** ~$0.39
- **Per session (generic/Haiku):** ~$0.12

Blended across 3 conditions (1 Sonnet + 2 Haiku):
- ~(0.39 + 0.12 + 0.12) / 3 × 24 sessions ≈ **$5.04**

Conservative estimate (all Sonnet): 24 × $0.39 = **$9.36**

Budget: **$10**

---

## Known Limitations

- **Single topic**: Results are not generalizable. The topographic map topic was selected for
  highest evaluator–ground-truth correlation, which may make it an outlier. Additionally,
  topographic map reading is highly spatial — human tutors naturally provide structured visual
  descriptions (narrative behavior), which a slim domain map types as `concept`. The Socratic
  tutor may be at a slight disadvantage on this specific topic.
- **NAC circularity**: `pilot_composite` penalizes non-Socratic tutors for not being Socratic.
  Always use `nac_adjusted_composite` for comparing pedagogical effectiveness across conditions.
- **Slim domain maps**: No `knowledge_type`/`reference_material`. All KCs treated as `concept`
  type by the LCQ classifier and by `SocraticTutor`. LCQ is excluded from composite for this
  reason. KFT and PR are unaffected.
- **Student confound**: Jamie (Haiku) responds to all three tutors. If `bare-map` explains
  content directly and Jamie's subsequent responses improve as a result, KFT scores may
  reflect improved student answers rather than better tutor targeting. The smoke-test
  transcript spot-check (pre-flight item 6) is the mitigation.
- **MRQ sparsity**: Jamie has no seeded misconceptions. MRQ will likely be null for most or
  all sessions. This is expected — include it in output and note coverage in results.
- **PR at 10 turns**: Stall detection requires K=3 consecutive turns on the same KC.
  PR ≈ 1.0 for all conditions is a valid null result at this session length.
- **LCQ recorded but not used**: LCQ values in `sim_results.json` reflect "fraction of turns
  where the tutor used pure Socratic questioning" (since `warranted_type` is always `concept`).
  This is not the intended LCQ metric. Values are stored for reference but should not be
  interpreted as lesson calibration quality without enriched domain maps.
- **Low statistical power**: n=8 gives ~±0.07 CI. The decision table above specifies how
  to interpret inconclusive results rather than forcing a conclusion from underpowered data.
