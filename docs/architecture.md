# System Architecture

## Data Flow (Full Pipeline)

```
Wikipedia URL
    → fetch article (REST API)
    → domain mapper (Claude) → domain map (stored in DB)
    → pre-session assessment (2–4 questions) → initial BKT L0s
    → tutoring session (Socratic dialogue, N turns)
    → transcript + tutor state snapshots (stored in DB)
    → analyze_transcript() [post-hoc]
    → evaluation metrics
```

## Component: SocraticTutor (`tutor_eval/tutors/socratic.py`)

The core tutor. Wraps the Anthropic API with:
- A system prompt describing Socratic pedagogy
- A cached domain map block (reduces token cost ~54%)
- Session state (current phase, concept, student mastery estimates)
- A response guardrail (`_enforce_socratic`) that detects and rewrites direct answers via a fast Haiku call

**Stateless server usage**: `SocraticTutor(state=saved_state)` reconstructs tutor from a saved DB snapshot. `session_state()` serializes current state for storage.

**Prompt caching**: The domain map is sent as a separate system block with `cache_control: {"type": "ephemeral"}`. Anthropic caches it server-side for 5 minutes; subsequent turns within a session pay ~10% of normal input cost for that block.

**Response guardrail**: After generating a reply, a Haiku call checks for direct answers. If `FAIL:`, the revised response is used. Both the original and revised responses are saved to DB for NAC metric computation.

```python
# Reconstruct from saved state
tutor = SocraticTutor(topic=topic, domain_map=domain_map, state=saved_state)

# One turn
reply = tutor.respond(student_message, client=anthropic_client)

# Serialize for storage
state = tutor.session_state()
```

## Component: BKTEvaluator (`tutor_eval/evaluation/bkt.py`)

Tracks student knowledge over time using Bayesian Knowledge Tracing.

**Parameters (hand-tuned, empirically validated against Junyi dataset):**
- P_L0: absent=0.10, partial=0.50, mastered=0.90 (initialized from profile or pre-assessment)
- P_T (transit) = 0.10
- P_G (guess) = 0.25
- P_S (slip) = 0.10
- Mastery threshold: p_mastered ≥ 0.7

**KC filtering**: Each classify call receives only the ~9 most relevant KCs (target + direct prerequisites + misconception KCs), not all 38+.

**Two constructor paths:**
```python
# Simulation path
BKTEvaluator(profile=profile, kg=kg)

# Server path (human sessions)
BKTEvaluator(bkt_states={"kc_id": 0.3, ...}, target_kcs=["kc1", "kc2"])
```

## Component: Domain Map

Produced once per Wikipedia article by `domain-mapper` (Claude opus call). Stored in DB.

```json
{
  "topic": "DNA",
  "core_concepts": [
    {
      "concept": "Nucleotides",
      "description": "...",
      "prerequisite_for": ["Double Helix"],
      "depth_priority": "essential"
    }
  ],
  "recommended_sequence": ["Nucleotides", "Double Helix", ...],
  "common_misconceptions": [...],
  "checkpoint_questions": [
    {"after_concept": "Nucleotides", "question": "...", "what_a_good_answer_demonstrates": "..."}
  ]
}
```

`build_kg_from_domain_map()` in `webapp/services/domain_cache.py` translates this to BKTEvaluator format: `{"kcs": [...], "edges": [...]}`.

## Component: Pre-Session Assessment

A brief conversation at session start to initialize BKT L0 values for human students.

**Format (hybrid approach):**
1. Fixed opener: "Before we begin, briefly describe what you already know about [topic]."
2. Up to 3 targeted follow-up questions about foundational KCs (the prerequisites-of-prerequisites from the domain map's `recommended_sequence`)
3. Total: 2–4 questions, regardless of article size

The student's responses are classified by the evaluator to produce L0 estimates, which are stored in `bkt_states` (DB table) and used as the starting model for post-hoc analysis.

## Component: Post-Hoc Evaluation (`analyze_transcript()`)

Runs after a session is complete. Takes:
- Transcript (all turns, with pre/post-guardrail tutor responses)
- Domain map (KC graph)
- Initial BKT L0s (from pre-assessment)
- Per-turn tutor state snapshots

Steps through transcript turn-by-turn:
1. Classify student response → BKT observation class
2. Update BKT state
3. Compute frontier (unmastered KCs whose prerequisites all ≥ 0.7)
4. Evaluate tutor question against frontier (KFT)
5. Detect tangent turns; evaluate tangent handling (THQ)

## Webapp Session Lifecycle

```
POST /api/articles/resolve          → fetch article, queue domain map generation
GET  /api/articles/{id}             → poll domain_map_status (pending → ready)
POST /api/sessions                  → create session (requires domain_map ready)
POST /api/sessions/{id}/assessment/start   → begin pre-assessment
POST /api/sessions/{id}/assessment/answer  → submit assessment answer
POST /api/sessions/{id}/assessment/complete → finalize L0s, transition to active
POST /api/sessions/{id}/turn        → one tutoring exchange
GET  /api/sessions/{id}/transcript  → retrieve full conversation
POST /api/sessions/{id}/end         → mark session completed
POST /api/export/{id}/analyze       → run post-hoc evaluation
```

## Database Schema (key tables)

| Table | Purpose |
|-------|---------|
| `users` | registered + anonymous users, JWT auth |
| `articles` | Wikipedia articles, domain map JSON, status |
| `sessions` | tutoring sessions, tutor_state_snapshot (per-turn JSON), status |
| `turns` | one row per exchange: student message, raw tutor reply, guardrail reply, KC classifications |
| `bkt_states` | per (user, article, KC): current p_mastered |
| `assessments` | pre-session assessment Q&A, initial L0 estimates |
| `retention_schedule` | spaced repetition scheduling (future) |
