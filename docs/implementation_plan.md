# Implementation Plan

## Status Legend
- ✅ Complete
- 🔄 In Progress
- ⬜ Not Started

---

## tutor_eval Modifications (Blockers for Webapp)

✅ `BKTEvaluator` accepts `bkt_states` + `target_kcs` directly (server path, no profile/kg required)
✅ `SocraticTutor.__init__` accepts `state: dict | None` for stateless server reconstruction
✅ Domain map moved to cached system prompt block (prompt caching, ~54% token cost reduction)
✅ `_enforce_socratic()` response guardrail (Haiku call, detects and rewrites direct answers)

---

## Webapp Phases

### Phase 0 — Project Scaffolding ✅
- `webapp/app.py` — FastAPI factory with lifespan, CORS, router mounting
- `webapp/config.py` — Central env var configuration
- `webapp/db/__init__.py` — Async SQLAlchemy engine, `init_db()`, `get_db()`
- `webapp/db/models.py` — 7 ORM tables: User, Article, Session, Turn, BKTStateRow, Assessment, RetentionSchedule
- All route files stubbed with 501 responses

### Phase 1 — Wikipedia Article Ingestion ✅
- `webapp/services/wikipedia.py` — `fetch_article(url_or_title)` → WikiArticle
- `webapp/services/domain_cache.py` — DB-backed domain map cache, `build_kg_from_domain_map()`
- `webapp/api/articles.py` — `POST /resolve`, `GET /{id}`, `GET /featured/today`
- Background domain map generation via `_compute_domain_map_bg`

### Phase 2 — Auth ✅
- `webapp/api/auth.py` — register, login, anonymous
- pbkdf2_sha256 password hashing (not bcrypt — avoids 72-byte limit)
- JWT tokens via python-jose

### Phase 3 — Turn Hot Path ✅
Core of the webapp. One tutoring exchange:
1. Receive student message + API key header
2. Load session + article from DB; enforce turn budget (402 if exhausted)
3. Reconstruct `SocraticTutor(topic, domain_map, state=tutor_state_snapshot)`
4. Call `tutor.respond(message)` via `asyncio.to_thread`
5. Save Turn rows: student message + tutor reply (raw pre-guardrail + guardrail reply + tutor state snapshot)
6. Update `session.tutor_state_snapshot`, increment `turn_count` and token counters
7. Return guardrail reply to client

Key design decision: **no BKT or evaluation during the turn**. Pure data collection.

### Phase 4 — Pre-Session Assessment ✅
Brief protocol at session start to initialize BKT L0 values for human students.

**Format:**
- Fixed opener: "Before we begin, briefly describe what you already know about [topic]."
- Up to 3 targeted follow-ups targeting foundational KCs (graph roots — no upstream dependencies)
- Short-circuit: if opener classified as "mastered", only 1 follow-up
- Max 4 questions total

**Implementation:**
- `POST /api/sessions/{id}/assessment/start` — idempotent, creates opener row, pre-computes follow-up queue stored in `session.tutor_state_snapshot`
- `POST /api/sessions/{id}/assessment/answer` — Haiku classification, creates next question row or signals `assessment_complete`
- `POST /api/sessions/{id}/assessment/complete` — 4-phase L0 propagation, upserts BKTStateRows, transitions session to `active`

### Phase 5 — Frontend ✅
Single-page web UI (plain HTML/CSS/JS, no build step, no framework).

**Files:**
- `webapp/templates/index.html` — single HTML shell
- `webapp/static/style.css` — dark slate theme, matching visualize.html
- `webapp/static/auth.js` — token management, BYOK key storage, login/register/anonymous
- `webapp/static/article.js` — URL resolution, domain map polling
- `webapp/static/assessment.js` — assessment loop (promise-based one-shot input handler)
- `webapp/static/chat.js` — DOM manipulation for message bubbles, thinking indicator
- `webapp/static/graph.js` — knowledge graph panel (viz.js + svg-pan-zoom); see Phase 6
- `webapp/static/app.js` — state machine orchestration, `apiFetch` utility

**State machine phases:** `auth` → `article` → `chat (assessment)` → `chat (tutoring)` → `ended`

**Auth flow:**
- First visit: sign in or register (anonymous removed)
- Consent checkbox (not pre-checked) required at registration: "Conversation transcripts are collected to evaluate tutor performance."
- JWT token stored in `localStorage`

**Lesson catalog UI:**
- Card list replaces the previous Wikipedia URL form
- Clicking a card immediately starts or resumes the session (no two-step confirm)
- "← Lessons" back button returns to catalog without ending session on backend

**Credit system:**
- Server uses its own `ANTHROPIC_API_KEY` — no BYOK
- Users start with 0 credits; superusers exempt
- Credits decremented per tutoring turn; HTTP 402 when exhausted
- Admin endpoint: `POST /api/admin/users/{user_id}/credits`

**Backend additions implemented:**
- `GET /` route in `app.py` serves `index.html` via Jinja2Templates
- `Session.total_input_tokens`, `Session.total_output_tokens` columns added
- `User.consented_at` column added
- `User.credits_remaining` column added
- HTTP 402 response when credits exhausted
- Credit enforcement + token accumulation in `post_turn`

### Phase 6 — Knowledge Graph Panel ✅
Live KC knowledge graph in the chat phase showing the tutor's model of student understanding.

**Implementation:**
- `webapp/static/graph.js` — `KCGraph` module with `init(domainMap)`, `setBKT(snapshot)`, `setTutorState(state)`
- `GET /api/sessions/{id}/graph-state` — returns `domain_map`, `bkt_snapshot`, `tutor_state`
- `TurnResponse` enriched with `tutor_state` field so the observations panel updates each turn
- Split chat layout: 46% graph panel (left) + 54% chat panel (right); graph panel hidden on mobile

**Rendering**: viz.js (Graphviz WASM) renders DOT-language source; svg-pan-zoom adds pan/zoom. Node fill colour from BKT assessment (blue → amber → green gradient). Node border colour driven by tutor's `current_concept_index`: white = active, green = covered, blue = not yet reached.

**Domain map prerequisite fix pass** (`_fix_prerequisite_references` in `domain_cache.py`): a Haiku LLM call canonicalises `prerequisite_for` references so they exactly match `core_concepts` concept names. Runs once at domain-map creation time; result is cached permanently. Prevents disconnected graph nodes caused by LLM name inconsistency.

**Known limitation**: See `KNOWN_ISSUES.md` KI-001 — two sequential LLM calls (Sonnet + Haiku) during domain map generation can exceed the frontend's 120 s poll timeout.

### Phase 7 — Spaced Repetition ⬜
Use `retention_schedule` table to schedule review sessions.
- Based on BKT mastery estimates + forgetting curves

### Phase 8 — Post-Hoc Evaluation (`analyze_transcript()`) ⬜
Core evaluation function. Takes transcript + domain map + initial L0s + tutor state snapshots.

Computes:
- NAC (non-answer compliance, pre-guardrail)
- KFT (knowledge frontier targeting, per-turn then averaged)
- MRQ (misconception response quality)
- THQ (tangent handling quality — only if tangent turns exist)
- RS (robustness, meta-metric)
- Composite score

Exposed via `POST /api/export/{session_id}/analyze`.

### Phase 9 — Export ⬜
`POST /api/export/{session_id}` — export transcript in a format consumable by `analyze_transcript()`.

Includes: domain map, initial BKT L0s, full turn-by-turn transcript with pre/post-guardrail responses, tutor state snapshots.

### Phase 10 — Deployment ⬜
- Switch from aiosqlite to PostgreSQL
- Docker / cloud deployment
- HTTPS, secrets management

---

## Test Coverage

104 tests passing as of last commit:
- `tests/tutor_eval/test_bkt.py` (23 tests) — BKT math, frontier, KC filtering
- `tests/tutor_eval/test_socratic_tutor.py` (17 tests) — state, caching, guardrail
- `tests/webapp/test_wikipedia.py` (14 tests) — URL parsing, HTTP mocks
- `tests/webapp/test_api.py` (50 tests) — auth, articles, sessions, assessment (in-memory SQLite)

All new features should include corresponding tests. Run with:
```bash
pytest
```

---

## Resolved Design Decisions

| Question | Decision |
|----------|----------|
| What data does the evaluator need? | Domain map + pre-assessment L0s + transcript + per-turn tutor state snapshots |
| NAC: log pre- or post-guardrail? | Both — NAC computed against pre-guardrail |
| KFT: per-response or aggregate? | Per-turn, computed by evaluator during post-hoc analysis |
| Tangent handling | Natural handling during session; evaluator partitions on-topic/tangent; THQ metric |
| Pre-assessment format | Hybrid: fixed opener + ≤3 targeted follow-ups on foundational KCs; max 4 questions |
| API key model | Server-hosted `ANTHROPIC_API_KEY`; no BYOK |
| Cost / rate limiting | Credit system: 1 credit per turn; superusers exempt; token counts accumulated silently |
| User accounts | Full registration (email + password) + anonymous; learning history linked to accounts is future work |
| Data collection consent | Checkbox at registration (not pre-checked); plain-language disclosure; satisfies PIPEDA + basic GDPR |
| Prompt injection security | No real harm possible (text-only API, no tools); guardrail handles Socratic compliance; monitor failure modes |
| Scaling cost model | BYOK for MVP; reassess with hosted key + subscription once product validated |
