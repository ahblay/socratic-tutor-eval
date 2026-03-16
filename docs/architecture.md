# System Architecture

## Data Flow (Full Pipeline)

```
Wikipedia URL
    → fetch article (REST API)
    → domain mapper (Sonnet) → domain map
    → prerequisite-fix pass (Haiku) → canonicalised domain map (stored in DB)
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

Produced once per Wikipedia article by a two-step LLM process. Stored in DB.

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

**Two-step generation** (`webapp/services/domain_cache.py`):
1. **Sonnet call** (`compute_domain_map`): generates the full domain map JSON from article text.
2. **Haiku fix pass** (`_fix_prerequisite_references`): canonicalises all `prerequisite_for` entries so they exactly match a `concept` name in `core_concepts`. This is necessary because the Sonnet call sometimes writes concept names inconsistently across sections, which causes edges to be silently dropped when building the KC graph. The fix pass has a fast path (skips if already consistent), a sanity check (rejects the fixed map if the concept set changed), and a fail-safe (returns the original on any error). One-time cost per article; result is cached permanently.

`build_kg_from_domain_map()` translates the domain map to BKTEvaluator format: `{"kcs": [...], "edges": [...]}`.

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

## Component: Knowledge Graph UI (`webapp/static/graph.js`)

The chat phase includes a live knowledge graph panel that visualises the tutor's model of the student's understanding. The graph is populated from assessment results and does **not** update turn-by-turn during tutoring.

**Rendering stack:**
- **viz.js** (`@viz-js/viz@3.25.0`, CDN) — Graphviz compiled to WASM. Renders the KC dependency graph from a DOT-language string.
- **svg-pan-zoom** (`svg-pan-zoom@3.6.1`, CDN) — adds pan and pinch-to-zoom to the rendered SVG.
- `_viz` and `_panZoom` are module-level singletons; `_panZoom.destroy()` is called before each re-render to prevent memory leaks.

**Node colouring**: `knowledgeColor(p)` maps p_mastered (0–1) to a hex colour along a blue → amber → green gradient. Frontier nodes (all prerequisites mastered, this concept not yet mastered) get a thicker amber border.

**Data source** (`GET /api/sessions/{id}/graph-state`):
```json
{
  "domain_map":    { "core_concepts": [...], ... },
  "bkt_snapshot":  { "<kc-slug>": 0.42, ... },
  "tutor_state":   { "student_understanding": [...], "frustration_level": "..." }
}
```
- `domain_map`: article's KC graph — used to build the DOT source and the mastery bar list.
- `bkt_snapshot`: per-KC p_mastered values from the assessment — used to colour nodes and fill bars.
- `tutor_state`: tutor's latest observations — shown as chips in the "Tutor observations" panel.

`app.js` calls `_loadGraphState()` twice: once when entering the chat phase and once at the start of tutoring (after assessment writes the initial BKT snapshot).

Each turn response (`POST /sessions/{id}/turn`) also returns a `tutor_state` field so the observations panel updates after every student message without a full graph refresh.

## Component: BYOK API Key Handling

Users provide their own Anthropic API key. It is passed as a custom request header (`X-API-Key`) on session creation and each turn request. The server reads it per-request and passes it to the `SocraticTutor` — it is never written to the DB.

**Turn budget enforcement**: `Session.max_turns` is set at session creation. `POST /sessions/{id}/turn` checks `session.turn_count < session.max_turns` before invoking the tutor. Returns HTTP 402 if budget exhausted.

**Token tracking**: After each tutor API call, `session.total_input_tokens` and `session.total_output_tokens` are incremented from the response's `usage` field. Stored for cost analysis; not shown to users in MVP.

**Security note**: The API key is transmitted over HTTPS and stored in browser `localStorage`. The server must never log request headers. This is the accepted BYOK risk profile.

## Webapp Session Lifecycle

```
POST /api/articles/resolve          → fetch article, queue domain map generation
GET  /api/articles/{id}             → poll domain_map_status (pending → ready)
POST /api/sessions                  → create session (requires domain_map ready)
POST /api/sessions/{id}/assessment/start   → begin pre-assessment
POST /api/sessions/{id}/assessment/answer  → submit assessment answer
POST /api/sessions/{id}/assessment/complete → finalize L0s, transition to active
POST /api/sessions/{id}/turn        → one tutoring exchange
GET  /api/sessions/{id}/graph-state → domain map + BKT snapshot + tutor state (for graph panel)
GET  /api/sessions/{id}/transcript  → retrieve full conversation
POST /api/sessions/{id}/end         → mark session completed
POST /api/export/{id}/analyze       → run post-hoc evaluation
```

## Database Schema (key tables)

| Table | Purpose |
|-------|---------|
| `users` | registered + anonymous users, JWT auth, consent timestamp |
| `articles` | Wikipedia articles, domain map JSON, status |
| `sessions` | tutoring sessions, tutor_state_snapshot, max_turns, token usage counters |
| `turns` | one row per message: student message OR tutor reply (raw + guardrail) + tutor state snapshot |
| `bkt_states` | per (user, article, KC): current p_mastered |
| `assessments` | pre-session assessment Q&A, initial L0 estimates |
| `retention_schedule` | spaced repetition scheduling (future) |

**Implemented schema additions (Phase 5):**
- `sessions.max_turns` (Integer) — turn budget set at session creation
- `sessions.total_input_tokens` (Integer, default 0) — accumulated from API responses
- `sessions.total_output_tokens` (Integer, default 0) — accumulated from API responses
- `users.consented_at` (DateTime, nullable) — timestamp of data collection consent

**BYOK wiring (implemented)**: `post_turn` extracts `X-API-Key` and passes it to `SocraticTutor(api_key=...)`. Assessment `answer_question` constructs a per-request `AsyncAnthropic(api_key=...)` if the header is present. Domain map generation uses the server key exclusively (shared cached resource).
