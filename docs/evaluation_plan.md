# Post-Hoc Evaluation Plan

This document records the metric design decisions reached through design review on 2026-03-24.
It supersedes the metric specifications in `evaluation.md` and `Socratic_Tutor.pdf` where they
conflict. `evaluation.md` remains as historical context.

---

## Overview

All evaluation is **post-hoc**: the evaluation pipeline runs after a session is complete,
replaying the transcript to reconstruct BKT states and compute metrics. Nothing is evaluated
live during tutoring.

The primary use case is webapp transcripts (real human students). Simulation JSONL files
(synthetic student profiles) are also supported.

---

## Inputs

A transcript subject to post-hoc analysis provides:

| Field | Source | Notes |
|---|---|---|
| Conversation turns | Webapp DB / simulation JSONL | `{student_message, tutor_response}` per turn |
| Pre-session assessment turns | Webapp DB | Used to initialize BKT L0 values |
| Response-reviewer verdicts | Webapp DB / simulation JSONL | Per-turn `{verdict: pass/warn/fail}`; absent in some sessions |
| Tutor session state snapshots | Webapp DB / simulation JSONL | `session_state()` per turn; Claude tutor only |
| Domain map | Webapp DB (cached) | KC graph, prerequisite edges, misconception list |

---

## BKT Initialization

BKT state for each KC must be initialized before replaying the transcript.

**Primary method — pre-session assessment:**
Run the BKT evaluator over the assessment turns (the brief knowledge-probe exchange before
the lesson begins) to derive per-KC L0 values. These turns are specifically designed to
elicit what the student already knows, giving a more grounded starting point than any
fixed default.

**Limitation:** The assessment has a maximum question count and students are sometimes
reticent. The initialized state may underestimate prerequisite knowledge.

**Mitigation:** BKT self-corrects quickly. From L0 = 0.10, two `strong_articulation`
observations are sufficient to cross the mastery threshold. For the first 2–3 turns of a
lesson, KFT scores may be slightly distorted by an underestimated frontier; this error
is bounded and accepted.

**Fallback (no assessment data):** Initialize prerequisite KCs at p = 0.90 (assumed
mastered) and target KCs at p = 0.10 (absent). This gives a reasonable frontier at
turn 1 without transcript evidence.

---

## Metrics

### NAC — Non-Answer Compliance

**What it measures:** Whether the tutor avoided giving direct answers.

**How it is computed:** Per-turn LLM classification (Haiku) of each tutor response.
Verdict: `compliant` or `violation`.

```
NAC = compliant_turns / total_turns_with_verdict
```

When the response-reviewer was disabled for a session, set `NAC = 1.0` (evaluation
bypassed by design, not a tutor failure).

**Role in the composite:** NAC is a **multiplicative wrapper**, not an additive term.

```
Session score = NAC × f(KFT, PR, MRQ)
```

Rationale: NAC compliance is a precondition for Socratic tutoring, not a graded
dimension of it. A tutor with NAC = 0.60 is not a mediocre Socratic tutor — it is
a tutor that failed the defining constraint 40% of the time. Folding this into a
weighted average allows it to be compensated by high KFT, which is misleading.
As a multiplier, persistent violations depress the entire score regardless of other
metric values.

---

### KFT — Knowledge Frontier Targeting

**What it measures:** Whether the tutor's questions target KCs at the current boundary
of the student's knowledge — not drilling already-mastered content, not jumping ahead
of unmet prerequisites.

**How it is computed:** Per-turn LLM classification (Haiku) of each tutor response,
determining which KC the question targets and comparing to the BKT frontier at that
moment.

**Graded per-turn scoring:**

| Condition | Turn score |
|---|---|
| Targeted KC is on the frontier (unmastered, prerequisites met) | 1.0 |
| Targeted KC is already mastered (p ≥ 0.7) | 0.0 |
| Targeted KC's prerequisites not yet met | 0.0 |
| Off-map / no clear KC target | 0.0 |

```
KFT = mean of per-turn scores across all tutor turns
```

**Note on mastered-KC scoring:** The previous design assigned 0.3 for targeting a
mastered KC. This is removed. The rationale for harsher treatment: repeatedly probing
content the student already understands is frustrating and erodes trust in the tutor's
competence. A single misfired turn is noise, but the pattern is captured by PR (see below).
Setting the per-turn score to 0.0 ensures KFT reflects the cost of this failure accurately.

---

### PR — Progression Rate

**What it measures:** Whether the tutor avoids unproductive drilling — persistently
questioning a KC when neither the student's understanding is advancing nor the question
is appropriate.

Two distinct failure modes are captured under a single mechanism:

- **Shape 1 — Mastery drilling:** The tutor repeatedly targets a KC the student already
  understands (p ≥ 0.7). This is frustrating and wastes session time.
- **Shape 2 — Stall drilling:** The tutor repeatedly targets a frontier KC but the
  student's BKT score is not improving. The tutor has failed to change approach in
  response to a lack of progress.

**Stall definition:** A stall is a run of K ≥ 3 consecutive turns where:
- The same KC is being targeted (from KFT's per-turn classification), AND
- No meaningful BKT progress occurs, defined as:
  - **Shape 1:** KC has p_mastered ≥ 0.7 throughout the run
  - **Shape 2:** KC has p_mastered < 0.7 and Δp_mastered < δ = 0.05 per turn

**Misconception exemption:** If the BKT evaluator classifies the student's response as
`misconception` on any turn within a run, that turn resets the stall counter. Surfacing
a misconception is a productive outcome — the tutor should not be penalized for probing
it across multiple turns.

```
PR = 1 − (turns_in_stall / total_turns)
```

**Design parameters:**
- K = 3 (minimum consecutive turns before a stall is declared)
- δ = 0.05 (minimum per-turn BKT delta to count as progress)

These are initial heuristic values. Calibration against real session data is expected
to refine them.

**Implementation dependency:** PR reuses KFT's per-turn KC-targeting classification.
No additional LLM calls are required. The BKT delta component comes from the student
observation classifier (independent of KFT), giving PR partial independence from KFT
even though they share the KC-identification step.

---

### MRQ — Misconception Response Quality

**What it measures:** When the BKT evaluator independently detects a student misconception,
whether the tutor's subsequent response probes it Socratically rather than ignoring it or
correcting it directly.

**Source of misconceptions:** BKT evaluator observations only (`observation_class = misconception`).
This is an independent third-party signal. The tutor's own `accuracy_issues_open` and the
domain map's `common_misconceptions` are not used as inputs, to avoid circularity (evaluating
the tutor's response based on the tutor's own detection).

**How it is computed:** For each turn where the BKT evaluator flags a misconception, an LLM
classifier (Haiku) checks whether the tutor's next response targets that KC with a Socratic
probe rather than a direct correction or no response.

```
MRQ = targeted_probe_turns / total_misconception_turns
```

MRQ is `None` when no misconceptions were detected in the session (e.g. a student with no
activated misconceptions).

**Role in the composite:** Conditional additive adjustment. MRQ has no effect when no
misconceptions arise; when they do, it can swing the composite score by up to ±7.5
percentage points (15 points total range):

```
MRQ_adjustment = 0.15 × (MRQ − 0.5)    if misconceptions present
               = 0.0                     otherwise
```

At MRQ = 1.0: +0.075 bonus
At MRQ = 0.5: no effect
At MRQ = 0.0: −0.075 penalty

Rationale: unaddressed misconceptions are a genuine failure (they corrupt downstream
learning), not merely a missed opportunity. The symmetric formulation around 0.5 reflects
that random chance on an ambiguous classifier would produce MRQ ≈ 0.5 with no effect.

---

### LCQ — Lesson Calibration Quality *(diagnostic only, excluded from composite pending calibration)*

**What it measures:** Whether the tutor's choice of response type (Socratic questioning,
direct provision, or scaffolded provision) was appropriate for the specific student being
taught. A tutor that over-scaffolds an expert student or under-scaffolds a novice is
making a pedagogical mismatch that KFT, PR, and NAC cannot detect.

**Response type taxonomy:** Each tutor turn is classified into one of three behavioral types:
- `concept` — pure Socratic questioning; no information provided, student is guided to reason
- `convention` — single factual statement followed by an application question; appropriate for
  arbitrary standards the student cannot derive (e.g., syntax rules, regulatory definitions)
- `narrative` — structured content block provided before asking the student to reason about it;
  appropriate when the framework itself is the prerequisite for productive reasoning

**How it is computed:** For each tutor turn, two independent LLM classifications (Haiku) are run:
1. **Observed type**: classify the tutor's actual response into `concept`, `convention`, or `narrative`
2. **Warranted type**: given the KC being targeted and the student's BKT state at that turn,
   classify which type would have been appropriate for this specific student

A turn is aligned when observed type matches warranted type. Misalignment indicates
over-scaffolding (narrative/convention when concept was warranted) or under-scaffolding
(concept when narrative/convention was warranted).

```
LCQ = aligned_turns / total_tutor_turns
```

**Student-specificity:** The warranted-type classifier receives the student's BKT state at
the time of the turn, not a generic description of the KC. This is what distinguishes LCQ
from comparing against the domain map's pre-assigned KC type — the domain map is generated
before the student is known and cannot account for expert students who could derive what
the domain mapper classified as narrative.

**Domain map independence:** LCQ requires only the transcript and BKT state. It does not
require access to the domain map's KC type labels, making it applicable to sessions from
any tutor implementation (including non-Claude tutors without exposed domain maps).

**Relationship to domain map:** When a domain map is available, comparing the domain map's
KC type against the warranted-type classifier output is a separate diagnostic for *lesson
plan quality* — measuring whether the domain mapper over- or under-scaffolded for this
student. This is feedback on the domain mapper, not on the tutor, and should not be
conflated with LCQ.

**Role in composite:** Excluded from the session composite pending empirical calibration.
LCQ captures a dimension orthogonal to KFT/PR/NAC/MRQ — a tutor can score well on all
four while systematically over-scaffolding expert students. Its weight in a future composite
revision should be informed by correlation with human preference judgements across sessions
with varied student expertise levels.

---

### TBA — Teacher Belief Accuracy *(diagnostic only, excluded from composite)*

Measures alignment between the tutor's internal phase estimate (`session_state`) and the
BKT evaluator's estimated phase. Only computable for Claude's SocraticTutor (which exposes
`session_state()`); unavailable for other tutors.

Excluded from the composite because:
1. It is not universally available across tutor implementations, making it unsuitable for
   cross-model comparison.
2. It is a second-order measure: it correlates two imperfect estimators rather than
   measuring tutoring behavior directly.
3. It assumes a single ground truth for the student's knowledge state, which only holds
   in simulation (where the student profile is explicit).

TBA is still computed and reported as a diagnostic for the Claude tutor specifically.
Low TBA often explains low KFT — the tutor is targeting the wrong region because its
student model is miscalibrated.

---

### RS — Robustness Score *(tutor-level meta-metric, not per-session)*

**What it measures:** Consistency of tutor behavior across varied sessions — different
students, different lesson content, different session lengths.

**Scope:** RS is computed at the **tutor level** (across multiple sessions for a given tutor
configuration), not per-session. It does not appear in the session composite formula.

**What RS measures consistency of:** NAC and PR. These metrics capture behaviors that should
be constant regardless of content difficulty:
- A tutor should never give direct answers regardless of topic (NAC)
- A tutor should always recognize and respond to stalls regardless of topic (PR)

KFT is excluded from RS because it depends on domain map quality and KC graph complexity,
which vary across topics. A tutor may score lower KFT on a harder topic not because it is
a worse tutor, but because the prerequisite structure is more complex.

**Formula (provisional):**
```
RS = 1 − CV(NAC, PR across sessions)
```

Where CV is the coefficient of variation (std / mean). Exact formulation deferred — requires
empirical data across varied sessions to calibrate.

**Note:** For webapp transcripts, the assumption of consistent student profiles across the
same lesson cannot be made. RS must be meaningful across varied students and content.
Further design work needed once baseline session data is collected.

---

## Session Composite Formula

```
Session score = NAC × (0.5 × KFT + 0.25 × PR + 0.25 × LCQ + MRQ_adjustment)
```

Where:
- `NAC ∈ [0, 1]` — set to 1.0 when the response-reviewer was disabled
- `KFT ∈ [0, 1]` — graded frontier targeting score
- `PR ∈ [0, 1]` — 1 minus the fraction of turns spent in a stall
- `LCQ ∈ [0, 1]` — fraction of turns where observed response type matched warranted type
- `MRQ_adjustment = 0.15 × (MRQ − 0.5)` if misconceptions present, else `0.0`

**Weight rationale:**
- KFT (0.5) is the primary signal — it directly measures the core Socratic skill of
  targeting the right KC for this student at this moment.
- PR (0.25) and LCQ (0.25) share equal weight. Both measure whether the tutor's
  *approach* was appropriate: PR at the persistence level (did it stall?), LCQ at the
  response-type level (did it scaffold correctly for this student?). Equal weighting
  reflects that they are measuring the same dimension of approach quality through
  independent lenses.
- PR retains its existing partial dependency on KFT's KC-targeting classification;
  LCQ is largely independent — its two classifiers (observed type and warranted type)
  do not rely on frontier position.
- All weights are initial heuristics. Calibration against human preference judgements
  (e.g. pairwise Bradley-Terry) is the correct long-term approach.

**Score range:** Scores above 1.0 are valid. A session with strong misconception handling
(MRQ near 1.0) can reach approximately 1.075. Treat 1.0 as "excellent" and scores above
it as "excellent with strong misconception handling" — analogous to an A+. No capping.

**Minimum session length:** Sessions with fewer than 8 turns are excluded from analysis.
Short sessions do not provide enough turns for stall detection (PR requires K=3 consecutive
turns to register a stall) or stable BKT estimates, and the composite score would be
dominated by noise.

---

## Known Limitations

**Single classifier dependency.** Both KFT and PR rely on a single LLM classifier to
identify which KC each tutor question targets. Systematic bias in the classifier would
affect both metrics simultaneously and in the same direction. Random noise is less
concerning (it averages out), but consistent misclassification of a particular question
style could distort the composite in ways that are not visible from the scores alone.
Mitigation: validate the classifier against human-labeled turns when sufficient data exists.
For cross-model comparison, classifier errors are consistent across all tutors, so
relative rankings should remain valid even if absolute scores are slightly off.

**Student confound in PR.** A tutor facing a genuinely stuck student on a hard KC will
accumulate stall turns even if its questioning approach is sound. PR is therefore not
fully independent of student ability. This is partially mitigated by the argument that
a good tutor should change approach after 3 unproductive turns — but the confound is real
and should be noted when interpreting PR scores across sessions with different students.

**BKT initialization uncertainty.** The pre-session assessment may incompletely capture
prerequisite knowledge, leading to an underestimated frontier in early turns. The effect
is bounded (2–3 turns) and BKT self-corrects, but it introduces noise in early-turn KFT
scores.

**TBA unavailability for non-Claude tutors.** TBA is excluded from the composite partly
because it is Claude-only. If future tutor implementations expose comparable session state,
TBA could be reconsidered.

**Non-consecutive KC repetition not captured by PR.** PR detects stalls over consecutive
turns. A tutor that re-asks about a KC the student demonstrated mastery of 20–30 turns
earlier is not caught by the stall detector. This failure mode (observed in real webapp
sessions) represents a distinct pattern from stalling — the tutor has effectively "forgotten"
what was established. A future metric or PR extension could track which KCs have been
sufficiently addressed and penalize re-targeting after a significant turn gap.

**LCQ warranted-type classifier reliability.** The warranted-type classification is an LLM
judgment conditioned on BKT state. At the concept/narrative boundary — KCs that a skilled
tutor could guide a student to derive but which are more efficiently provided — this
classification is ambiguous and classifier agreement with human raters may be low. LCQ
scores near 0.5 on sessions with many narrative-adjacent KCs should be interpreted with
caution.

---

## Observed Failure Modes — Metric Design Pending

These failure modes have been identified in real session transcripts but do not yet have
a formalized metric. They are recorded here for future design work and to accumulate
alongside patterns from additional transcripts before committing to a specific formulation.

---

### Factual Accuracy

**What it captures:** The tutor providing factually incorrect statements or using
misleading premises in hypothetical scenarios. Two distinct sub-types have been observed:

- **Direct factual error:** A tutor assertion that contradicts the source material or
  the domain under study.
- **False premise in questioning:** A hypothetical scenario constructed by the tutor
  that violates a rule or fact the student has already demonstrated understanding of,
  leading the student into a misleading line of reasoning. The student may or may not
  catch the error. Observed example: tutor posed a scenario where defective tablets were
  shipped before batch testing was complete — a GMP violation the student correctly
  identified and pushed back on.

**Why this is distinct from NAC:** NAC measures whether the tutor gives direct answers.
Factual accuracy measures whether the tutor's questions and statements are themselves
correct. A tutor can be fully NAC-compliant while asking questions built on false premises.

**Evaluation approach:** An LLM fact-checker with access to the session's source material
(Wikipedia article or domain map KC descriptions) could flag tutor turns containing
assertions that contradict the source. False premise detection is harder — it requires
understanding the logical structure of the hypothetical and cross-referencing it against
established facts in the session.

**Role in scoring:** Factual accuracy is a categorical concern rather than a graded
dimension — a tutor that actively misleads students has committed a more serious failure
than a tutor that simply performs poorly on KFT or LCQ. It likely belongs near NAC as
a multiplicative quality gate rather than an additive term, or as a hard flag that
invalidates a session score. Design pending.

---

### Affirmation Rate

**What it captures:** The fraction of tutor turns that open with an explicit confirmation
or paraphrase of the student's prior response before asking the next question. Examples
include "That's exactly right — ..." or "So you've identified that ..., now ..." These
affirmations signal to the student whether their answer was correct before the next
question is posed.

**Why this is not uniformly bad:** The appropriate level of affirmation is student- and
context-dependent. Students with lower confidence or earlier in a lesson may benefit from
explicit validation; expert students or students who are self-assured may find it
condescending or may use it to game the lesson by calibrating their confidence from the
tutor's response pattern rather than their own understanding. Persistent affirmation
removes productive uncertainty, which is a core mechanism of Socratic learning.

**Why this does not belong in the composite:** Unlike the other failure modes, high
affirmation rate is not categorically harmful. Its effect on learning depends on the
student's learning style and the session context. It is better treated as a **session
descriptor** — a reported characteristic that contextualizes the composite score rather
than a dimension to optimize. In future, it could become a configurable tutor parameter
adjusted per student preference.

**Measurement:** Straightforward to compute — an LLM classifier (Haiku) flags each tutor
turn as affirmative or non-affirmative at the opening. The session-level rate is the
fraction of affirmative turns. No threshold for "too high" is specified; the value is
reported and interpreted in context.

---

- **RS formula:** Exact formulation pending empirical data across varied sessions.
- **PR parameter calibration:** K=3 and δ=0.05 need validation against real session data.
- **Composite weight calibration:** Bradley-Terry pairwise preference model as long-term
  replacement for heuristic weights.
- **Multi-LLM comparison infrastructure:** `GenericSocraticTutor` (OpenAI-compatible tutor)
  and updated `simulation/run.py --model` flag. Deferred to a future session.
- **`analyze_transcript()` implementation:** Post-hoc analysis function and `score.py` CLI.
  Deferred to implementation phase after design is approved.
- **LCQ composite weight:** Requires correlation analysis against human preference judgements
  across sessions with varied student expertise levels before a weight can be justified.
- **Non-consecutive KC repetition metric:** Extension of PR or standalone metric to penalize
  re-targeting KCs already demonstrated as mastered more than N turns prior. Requires
  empirical data to set the turn-gap threshold N.
- **Domain map lesson plan quality diagnostic:** Comparing domain map KC types against
  LCQ's warranted-type classifier output to assess domain mapper calibration per student.
  Separate from LCQ; provides feedback on lesson planning quality rather than in-session
  tutor performance.
- **Factual accuracy metric/gate:** Determine whether factual accuracy functions as a
  multiplicative quality gate (like NAC) or a hard session flag. Requires designing a
  fact-checker prompt with source material access, and separately addressing false-premise
  detection in hypothetical scenarios. May be merged with or combined alongside other
  failure modes surfaced from additional transcripts.
- **Affirmation rate as session descriptor:** Finalize the Haiku classifier prompt for
  affirmation detection and determine what session-level reporting looks like. Decide
  whether affirmation rate should eventually become a configurable tutor parameter
  adjustable per student preference.
- **Student model sensitivity (crediting correct answers):** A tutor that fails to
  recognize mastery from correct but unusually phrased student responses will
  systematically underestimate the student's frontier, causing it to re-teach already-
  mastered KCs. This sits upstream of KFT, LCQ, and PR — errors here propagate into
  all three. Observed example: a student's correct, precise answer was misread as a
  question, causing unnecessary re-teaching. Metric design pending; likely a
  per-turn classifier that checks whether the tutor updated its student model
  appropriately given the student's response.
