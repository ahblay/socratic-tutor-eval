# Implementation Plan: `POST /api/import/sessions`

## Overview

This endpoint accepts a JSON body containing a raw transcript (raw-transcript-v1) and a pre-scored result (EvaluationResult.to_dict()), imports them into the DB, and returns the session ID so the analysis viewer immediately works. It lives in a new file `webapp/api/import_.py` and is mounted under `/api/import`.

---

## 1. Turn Numbering Analysis

Before writing anything else, this section is critical because it directly determines whether `get_analysis_view` works correctly after import.

### How the analyzer assigns turn_number

The `converter.py` function `prepare_analysis_input` assigns `turn_number = i + 1` (1-based sequential index) to every turn in the flat `turns` list — both student and tutor. The `analyze_transcript` function then copies that `turn_number` directly into `TurnResult.turn_number`.

In the a2 example (155 total turns, starts with a tutor turn): turn 1 = tutor, turn 2 = student, turn 3 = tutor, ... The result's `turn_results` contains only tutor turns with `turn_number` values 1, 3, 5, ..., 155.

### How `get_analysis_view` uses turn_number

```
turns_by_number = {t.turn_number: t for t in all_turns}   # keyed by turn_number
for tr in analysis["turn_results"]:
    tn = tr.get("turn_number")
    tutor_turn = turns_by_number.get(tn)         # looks up Turn row by turn_number
    for t in reversed(all_turns):
        if t.turn_number < tn and t.role == "user":
            student_message = t.content; break   # finds preceding user turn
```

This means the Turn rows stored in the DB must have `turn_number` values that are **exactly consistent** with what the scorer used when producing the result. If the scorer saw "turn 1 = tutor" and the DB has "turn 1 = tutor", the lookup `turns_by_number.get(1)` returns the correct Turn row.

### The constraint

The import endpoint must store Turn rows with `turn_number` equal to the sequential 1-based position of that turn in the `transcript["turns"]` list — the same numbering scheme `converter.py` uses. Specifically:

- Iterate `transcript["turns"]` with a 1-based index counter.
- Set `Turn.turn_number = index` (1, 2, 3, ...) regardless of role.
- Translate `role="student"` to `role="user"`.
- The analysis result's `turn_results[*].turn_number` must match these stored turn_numbers exactly.

**Potential issue:** The user's design decision states "assign turn_number sequentially (1, 2, 3, ...)". This is correct provided the scorer also used sequential 1-based numbering — and it did. The concern to flag: the scorer's `turn_number` comes from the `analysis_input["lesson_turns"]` that was fed to it, not recomputed at import time. The import endpoint receives **both** the transcript and the result; it must trust that the result's `turn_numbers` were produced from sequential 1-based indexing of `transcript["turns"]`. There is no mismatch if the result was produced by `converter.py + analyze_transcript` from the same transcript. If the result was hand-edited or produced differently, the numbers could be wrong — but that is the caller's responsibility; the endpoint cannot verify this without re-running the scorer.

**No structural issue.** The 1-based sequential approach is exactly what the system already uses and what `get_analysis_view` expects.

---

## 2. New File: `webapp/api/import_.py`

### 2a. Pydantic Request Schema

The endpoint accepts `Content-Type: application/json`. Define:

**`ImportSessionRequest`** (top-level body):
- `transcript: dict` — the raw-transcript-v1 object. Required top-level keys validated separately (not typed as a nested Pydantic model) to allow forward compatibility. The endpoint reads: `transcript["session_id"]`, `transcript["topic"]`, `transcript["domain_map"]` (nullable), `transcript["bkt_initial_states"]` (nullable dict), `transcript["turns"]` (list), `transcript.get("date")`.
- `result: dict` — the EvaluationResult.to_dict() object. Required fields validated: must contain `session_id`, `turn_results` (list).
- `article_id: str | None = None` — optional. If provided, link to this existing article instead of creating a stub.

**`ImportSessionResponse`** (response):
- `session_id: str`
- `article_id: str`
- `article_created: bool` — whether a new stub article was created
- `turn_count: int`
- `bkt_rows_written: int`

### 2b. Synthetic Page ID Scheme

For stub articles (when `article_id` is not provided), a deterministic negative `wikipedia_page_id` is needed.

**Scheme:** Take the SHA-256 hash of the `topic` string (UTF-8 encoded), extract the first 8 bytes as a big-endian unsigned 64-bit integer, reduce it modulo `10^15` to get a positive integer that fits comfortably in a signed 64-bit integer, then negate it.

```python
def _synthetic_page_id(topic: str) -> int:
    digest = hashlib.sha256(topic.encode("utf-8")).digest()
    as_int = int.from_bytes(digest[:8], byteorder="big")
    bounded = as_int % (10 ** 15)
    return -bounded if bounded != 0 else -1
```

This is deterministic (same topic always produces the same ID), guaranteed negative (no collision with Wikipedia's positive IDs), and compact. Collision on the 10^15-element hash space is negligible; the 409 error path handles it.

### 2c. Complete Endpoint Logic (step by step)

**Step 0 — Auth and superuser check**

Call `get_current_user`. Immediately call `_require_superuser(current_user)`.

**Step 1 — Validate the incoming payload**

Extract and validate:
- `session_id_from_transcript = body.transcript.get("session_id")` — must be non-empty string. If missing/null → 422.
- `topic = body.transcript.get("topic")` — must be non-empty → 422 if missing.
- `turns_raw = body.transcript.get("turns")` — must be a non-empty list → 422 if missing or empty.
- `bkt_initial_states_raw = body.transcript.get("bkt_initial_states") or {}` — dict keyed by kc_id.
- `domain_map_from_transcript = body.transcript.get("domain_map")` — nullable dict.
- `result_session_id = body.result.get("session_id")` — must be non-empty → 422.
- `turn_results = body.result.get("turn_results")` — must be a list (may be empty) → 422 if missing.

Validate each turn: `role` must be "student" or "tutor"; `content` must be non-empty. Return 422 listing which turns failed.

Cross-document consistency: `result_session_id` must equal `session_id_from_transcript`. If not → 422 "result.session_id does not match transcript.session_id".

**Step 2 — Collision check**

`SELECT Session WHERE Session.id == session_id_from_transcript`

If found → **HTTP 409** "Session {id} already exists".

**Step 3 — Resolve or create the Article row**

Branch A — `body.article_id` is provided:
- `SELECT Article WHERE Article.id == body.article_id`
- If not found → 404 "Article not found"
- Use this article. Set `article_created = False`.

Branch B — `body.article_id` is None:
- Compute `synthetic_page_id = _synthetic_page_id(topic)`
- `SELECT Article WHERE Article.wikipedia_page_id == synthetic_page_id`
- If found (same topic imported before): re-use it. Set `article_created = False`.
- If not found: create new Article:
  - `wikipedia_page_id = synthetic_page_id`
  - `canonical_title = topic`
  - `wikipedia_url = ""`
  - `summary = None`
  - `domain_map = domain_map_from_transcript`
  - `domain_map_status = "ready"` if domain_map is not None, else `"pending"`
  - `is_published = False`
  - `db.add(article)` then `await db.flush()` (need article.id before Turn creation)
  - Set `article_created = True`.

**Step 4 — Create the Session row**

Parse `transcript.get("date")` as "YYYY-MM-DD" → `datetime(year, month, day, tzinfo=utc)` for `started_at`. Fall back to `now()` if absent/invalid.

Create Session:
- `id = session_id_from_transcript` (transcript UUID becomes the PK directly)
- `user_id = current_user.id`
- `article_id = article.id`
- `started_at = parsed_date_or_now`
- `ended_at = None`
- `turn_count = count of "student" turns in turns_raw`
- `status = "completed"`
- `analysis = body.result` (stored as-is; already EvaluationResult.to_dict() format)
- `analysis_status = "ready"`
- `max_turns = None`
- `total_input_tokens = 0`
- `total_output_tokens = 0`
- `tutor_state_snapshot = None`

`db.add(session)` then `await db.flush()`.

**Step 5 — Create Turn rows**

```
for index, raw_turn in enumerate(turns_raw, start=1):
    db_role = "user" if raw_turn["role"] == "student" else "tutor"
    db.add(Turn(
        session_id = session.id,
        turn_number = index,
        role = db_role,
        content = raw_turn["content"],
        raw_content = None,
        reviewer_verdict = None,
        tutor_state_snapshot = None,
        evaluator_snapshot = None,
    ))
```

**Step 6 — Create BKTStateRow rows**

For each `(kc_id, state_dict)` in `bkt_initial_states_raw`:
- `SELECT BKTStateRow WHERE user_id == current_user.id AND article_id == article.id AND kc_id == kc_id`
- If row exists: **skip** (do not overwrite existing BKT state).
- If no row: insert new BKTStateRow with `p_mastered`, `knowledge_class`, `observation_history` from `state_dict`.

Track `bkt_rows_written` count (excludes skipped rows).

**Step 7 — Commit and return**

`await db.commit()`

Return:
```json
{
  "session_id": "...",
  "article_id": "...",
  "article_created": true,
  "turn_count": 155,
  "bkt_rows_written": 21
}
```

### 2d. Error Summary Table

| Condition | HTTP Status | Detail |
|---|---|---|
| Not authenticated | 401 | (from `get_current_user`) |
| Not superuser | 403 | "Superuser access required" |
| `transcript.session_id` missing | 422 | "transcript.session_id is required" |
| `transcript.topic` missing | 422 | "transcript.topic is required" |
| `transcript.turns` empty/missing | 422 | "transcript.turns must be a non-empty list" |
| Invalid turn role | 422 | "turn N: role must be 'student' or 'tutor'" |
| Invalid turn content | 422 | "turn N: content must be non-empty" |
| `result.session_id` ≠ `transcript.session_id` | 422 | "result.session_id does not match transcript.session_id" |
| `result.turn_results` missing | 422 | "result.turn_results is required" |
| Session already exists | 409 | "Session {id} already exists" |
| `body.article_id` not found | 404 | "Article not found" |
| SHA-256 `wikipedia_page_id` topic collision | 409 | "Synthetic wikipedia_page_id {id} already in use by a different article; provide article_id to link explicitly" |

### 2e. Module structure

```
webapp/api/import_.py
├── imports: APIRouter, Depends, HTTPException, BaseModel,
│            select, AsyncSession,
│            get_current_user, get_db,
│            Article, Session, Turn, BKTStateRow, User,
│            uuid, datetime, hashlib
├── router = APIRouter()
├── def _require_superuser(user) -> None
├── def _synthetic_page_id(topic: str) -> int
├── class ImportSessionRequest(BaseModel)
├── class ImportSessionResponse(BaseModel)
└── @router.post("/sessions", response_model=ImportSessionResponse)
    async def import_session(body, db, current_user)
```

---

## 3. Modified File: `webapp/app.py`

Line 52, change:
```python
from webapp.api import articles, sessions, assessment, auth, export, admin
```
to:
```python
from webapp.api import articles, sessions, assessment, auth, export, admin, import_
```

Add after the admin router line:
```python
app.include_router(import_.router, prefix="/api/import", tags=["import"])
```

Final path: `POST /api/import/sessions`

---

## 4. Edge Cases

- **Transcript starts with student turn:** viewer emits `student_message: null` for first frame — acceptable.
- **`bkt_initial_states` absent:** skip Step 6, return `bkt_rows_written = 0`. Viewer still works.
- **`domain_map` absent:** article stored with `domain_map = null`, `domain_map_status = "pending"`. Graph panel empty, frames still render.
- **`result.turn_results` has turn_number with no matching Turn row:** `tutor_response` in that frame = null. No server-side check possible; caller's responsibility.
- **Concurrent import of same session_id:** catch `IntegrityError`, rollback, return 409.
- **Unicode topic string:** SHA-256 over UTF-8 encoding handles this correctly.
- **`bounded == 0` in synthetic_page_id:** return -1 as sentinel (astronomically unlikely).
