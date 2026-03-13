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

### Phase 3 — Turn Hot Path ⬜
Core of the webapp. One tutoring exchange:
1. Receive student message
2. Load session + article from DB
3. Reconstruct `SocraticTutor(topic, domain_map, state=tutor_state_snapshot)`
4. Call `tutor.respond(message, client=anthropic_client)`
5. Save Turn row: student message, raw reply, guardrail reply, timestamp
6. Update `session.tutor_state_snapshot` with `tutor.session_state()`
7. Return guardrail reply to client

Key design decision: **no BKT or evaluation during the turn**. Pure data collection.

### Phase 4 — Pre-Session Assessment ⬜
Brief protocol at session start to initialize BKT L0 values for human students.

**Format:**
- Fixed opener: "Before we begin, briefly describe what you already know about [topic]."
- Up to 3 targeted follow-ups targeting foundational KCs (prerequisites-of-prerequisites)
- Max 4 questions total

**Implementation:**
- `POST /api/sessions/{id}/assessment/start` — generate opener, create Assessment row
- `POST /api/sessions/{id}/assessment/answer` — classify response, generate follow-up or finish
- `POST /api/sessions/{id}/assessment/complete` — compute L0s, store in `bkt_states`, transition session to `active`

### Phase 5 — Frontend ⬜
Minimal browser UI:
- Article URL input → poll for domain map readiness
- Pre-assessment chat interface
- Tutoring session chat interface
- Transcript view

Could also be a browser extension that overlays on Wikipedia pages.

### Phase 6 — Knowledge Map ⬜
Visual representation of student's current KC mastery across the domain map graph.
- Show mastered/frontier/unmastered KCs
- Update after each session (post-hoc BKT run)

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

76 tests passing as of last commit:
- `tests/tutor_eval/test_bkt.py` (23 tests) — BKT math, frontier, KC filtering
- `tests/tutor_eval/test_socratic_tutor.py` (17 tests) — state, caching, guardrail
- `tests/webapp/test_wikipedia.py` (14 tests) — URL parsing, HTTP mocks
- `tests/webapp/test_api.py` (22 tests) — auth, articles, sessions (in-memory SQLite)

All new features should include corresponding tests. Run with:
```bash
pytest
```

---

## Pending Design Questions

All resolved as of 2026-03-13:

| Question | Decision |
|----------|----------|
| What data does the evaluator need? | Domain map + pre-assessment L0s + transcript + per-turn tutor state snapshots |
| NAC: log pre- or post-guardrail? | Both — NAC computed against pre-guardrail |
| KFT: per-response or aggregate? | Per-turn, computed by evaluator during post-hoc analysis |
| Tangent handling | Natural handling during session; evaluator partitions on-topic/tangent; THQ metric |
| Pre-assessment format | Hybrid: fixed opener + ≤3 targeted follow-ups on foundational KCs; max 4 questions |
