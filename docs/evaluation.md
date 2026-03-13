# Evaluation Framework

## Overview

The evaluation framework measures how effectively the Socratic tutor guides student learning. Evaluation is always **post-hoc** — it runs after a session is complete, stepping through the transcript and applying BKT.

The full metric specification is in `Socratic_Tutor.pdf`. This document captures the design decisions made during implementation.

## Metrics

### NAC — Non-Answer Compliance
Measures how often the tutor avoids giving direct answers.

- Computed from transcript turns, not tutor state
- Both the **pre-guardrail** (raw Claude response) and **post-guardrail** (rewritten response) are logged
- NAC is computed against the pre-guardrail response — this reflects true tutor behavior, not the effectiveness of the safety filter
- Applies to all turns, including tangent turns

### KFT — Knowledge Frontier Targeting
Measures whether the tutor's questions target concepts at the boundary of what the student currently knows.

- Computed **per-turn** by the evaluator: "Did this question target a KC on the knowledge frontier at the time of asking?"
- Session score = average of per-turn scores
- The **frontier** = set of unmastered KCs whose prerequisites all have p_mastered ≥ 0.7
- Evaluator is responsible for this computation during post-hoc analysis; the webapp just saves the transcript
- Only computed for on-topic turns (see tangent handling below)

### MRQ — Misconception Response Quality
Measures whether the tutor addresses student misconceptions appropriately.

- Evaluator detects misconceptions in student responses (matched against domain map's `common_misconceptions`)
- Checks whether the tutor's next question addresses the misconception
- Applies to on-topic turns

### THQ — Tangent Handling Quality
Measures how effectively the tutor manages tangential student questions.

**Background**: Students learning a Wikipedia article may need to explore prerequisite concepts from other articles. These tangent turns still reflect learning and should not be ignored.

**How tangents are identified**: Evaluator classifies student input against the article's KC graph; turns that don't match any KC are flagged as tangents.

**Two sub-components:**
1. **Resolution detection**: Does the tutor correctly identify when a tangent question has been adequately addressed and steer back to the main topic? (vs. staying in tangent indefinitely or returning prematurely)
2. **Conceptual bridging**: When returning to the main topic, does the tutor explicitly connect tangent content to a KC in the article's domain map?

THQ is only computed for sessions containing tangent turns.

### RS — Robustness Score
Meta-metric computed across multiple sessions or student profiles. Not per-session.

### Composite Score
Weighted combination: KFT (0.4) + MRQ (0.3) + RS (0.2) + TBA (0.1).
TBA weight redistributed to other metrics if tutor state snapshots are unavailable.

## BKT Parameters

```
P_L0:      absent=0.10, partial=0.50, mastered=0.90
P_T:       0.10  (intentionally higher than Junyi-fitted ~0.001–0.097 to reflect tutoring efficiency)
P_G:       0.25
P_S:       0.10
Mastery threshold: p_mastered >= 0.7
```

Validated against Junyi Academy dataset (13.8M records). Closest match is "factors-multiples" topic (L0=0.303, T=0.097), the hardest topic in the dataset. See `Socratic_Tutor.pdf` for fitting details.

## Post-Hoc Analysis Pipeline

```python
analyze_transcript(
    transcript: list[Turn],       # all turns with pre/post-guardrail responses
    domain_map: dict,             # article KC graph
    initial_bkt_states: dict,     # KC → L0 from pre-session assessment
    tutor_state_snapshots: list,  # per-turn SocraticTutor.session_state() output
) -> EvaluationResult
```

Step-by-step:
1. Initialize BKTEvaluator with `initial_bkt_states` and KC graph derived from `domain_map`
2. For each turn in transcript:
   a. Classify student message → observation class (0–5)
   b. Update BKT state for relevant KCs
   c. Compute frontier
   d. Classify turn as on-topic or tangent
   e. If on-topic: evaluate KFT, MRQ
   f. If tangent: evaluate NAC, tangent resolution, bridging
3. Aggregate per-turn scores → session-level metrics
4. Return `EvaluationResult` with all metrics + per-turn breakdown

## Pre-Session Assessment

Before each tutoring session, a brief assessment initializes BKT L0 values for human students.

**Format:**
1. Fixed opener (always the same): "Before we begin, briefly describe what you already know about [topic]."
2. Up to 3 targeted follow-ups based on student summary + foundational KCs (prerequisites-of-prerequisites in `recommended_sequence`)
3. Maximum 4 questions total

**L0 initialization**: Evaluator classifies assessment responses to estimate knowledge per KC. L0 values propagate through the prerequisite graph (if a student knows a concept, prerequisites get higher L0; if not, dependents get lower L0).

Resulting L0 values are stored in `bkt_states` table and used as the starting model for post-hoc analysis.

## Simulated vs. Real Sessions

| | Simulation | Webapp (real humans) |
|--|--|--|
| Student | `StudentAgent` (LLM with bounded knowledge doc) | Real human |
| BKT init | Profile-based (tabula_rasa, partial, misconception) | Pre-session assessment |
| Evaluation | During or after simulation | Post-hoc only |
| KC graph | Junyi 38-node KC graph | Domain map per Wikipedia article |
| Use case | Tutor development + offline evaluation | Data collection |
