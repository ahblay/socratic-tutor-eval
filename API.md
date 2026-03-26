# API Reference — Socratic Tutor Webapp

Base URL: `http://localhost:8000`

---

## Authentication

All protected endpoints require a Bearer token obtained from `/api/auth/login`.

```bash
# Log in and capture the token
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=abeleromer@gmail.com&password=<your-password>" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo $TOKEN   # verify it printed
```

Use `$TOKEN` in all subsequent requests via `-H "Authorization: Bearer $TOKEN"`.

> **Note:** This is your webapp JWT — not your Anthropic API key. The server uses the Anthropic key internally.

---

## Admin Workflow: Adding a New Lesson

This is the primary workflow that requires curl. The UI only exposes the student-facing catalog.

### Step 1 — Resolve a Wikipedia article

Fetches Wikipedia metadata and kicks off domain map generation as a background task. Returns immediately; domain map builds asynchronously.

```bash
curl -s -X POST http://localhost:8000/api/articles/resolve \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://en.wikipedia.org/wiki/Cyclic_group"}'
```

**Response:**
```json
{
  "article_id": "...",
  "title": "Cyclic group",
  "wikipedia_url": "https://en.wikipedia.org/wiki/Cyclic_group",
  "summary": "...",
  "domain_map_status": "pending",
  "kc_count": 0
}
```

Save the `article_id` — you need it for all subsequent steps.

**Requirements:**
- Server must be running with `ANTHROPIC_API_KEY` set in its environment
- Your account must be a superuser (`is_superuser=1` in the DB)
- If the article already exists with `domain_map_status="ready"`, the background task is skipped

### Step 2 — Poll until the domain map is ready

The domain map takes 15–60 seconds to generate. Poll this endpoint until `domain_map_status` changes to `"ready"` and `kc_count > 0`.

```bash
curl -s http://localhost:8000/api/articles/<article_id> | python3 -m json.tool
```

Or in a loop:
```bash
watch -n 5 "curl -s http://localhost:8000/api/articles/<article_id> | python3 -m json.tool"
```

You can also check the DB directly:
```bash
sqlite3 /path/to/webapp.db \
  "SELECT canonical_title, domain_map_status, json_array_length(json_extract(domain_map, '$.core_concepts')) FROM articles ORDER BY rowid DESC LIMIT 5;"
```

**Possible `domain_map_status` values:**
| Value | Meaning |
|-------|---------|
| `pending` | Background task is running |
| `ready` | Domain map generated successfully |
| `failed` | Generation threw an exception (check server logs) |

If status is `ready` but `kc_count` is 0, the LLM produced an empty concept list. Delete the article row and retry with a more specific topic (see Troubleshooting).

### Step 3 — Publish the article

Makes the article visible in the student catalog. Fails with 409 if `domain_map_status != "ready"`.

```bash
curl -s -X POST http://localhost:8000/api/admin/articles/<article_id>/publish \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**Response:**
```json
{ "article_id": "...", "is_published": true }
```

### Step 4 — Verify it appears in the catalog

```bash
curl -s http://localhost:8000/api/articles | python3 -m json.tool
```

---

## Admin: User Management

### List all users

```bash
curl -s http://localhost:8000/api/admin/users \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

### Add credits to a user

```bash
curl -s -X POST http://localhost:8000/api/admin/users/<user_id>/credits \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"amount": 50}' | python3 -m json.tool
```

**Response:**
```json
{ "user_id": "...", "credits_remaining": 50 }
```

---

## Admin: Article Management

### Unpublish an article

```bash
curl -s -X POST http://localhost:8000/api/admin/articles/<article_id>/unpublish \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

### Resolve today's Wikipedia featured article

Resolves whichever article Wikipedia has featured today. Same behavior as `/resolve` — returns immediately and builds in background.

```bash
curl -s -X GET http://localhost:8000/api/articles/featured/today \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

---

## Public Endpoints (no auth required)

### List published articles

```bash
curl -s http://localhost:8000/api/articles | python3 -m json.tool
```

### Get a specific article (including unpublished)

```bash
curl -s http://localhost:8000/api/articles/<article_id> | python3 -m json.tool
```

---

## Auth Endpoints

### Register a new user

```bash
curl -s -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "password": "secret", "consented": true}' \
  | python3 -m json.tool
```

`consented` must be `true` or registration is rejected.

### Log in

Login uses form encoding (`application/x-www-form-urlencoded`), not JSON.

```bash
curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=user@example.com&password=secret" \
  | python3 -m json.tool
```

---

## Post-Hoc Evaluation

### Get analysis input for a session

Returns everything `analyze_transcript()` needs in one call: domain map, BKT initial
states (from assessment), assessment turns, and lesson turns with full evaluation metadata.

```bash
curl -s http://localhost:8000/api/admin/sessions/<session_id>/analysis-input \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**Response shape:**
```json
{
  "session_id": "...",
  "article_id": "...",
  "article_title": "...",
  "domain_map": { ... },
  "bkt_initial_states": {
    "<kc_id>": { "p_mastered": 0.1, "knowledge_class": "absent", "observation_history": [] }
  },
  "assessment_turns": [
    { "question_index": 0, "kc_id": "...", "question_text": "...", "user_answer": "...", "observation_class": "weak_articulation" }
  ],
  "lesson_turns": [
    { "turn_number": 1, "role": "tutor", "content": "...", "raw_content": "...", "reviewer_verdict": "pass", "tutor_state_snapshot": {...}, "evaluator_snapshot": null }
  ]
}
```

`bkt_initial_states` is empty when no assessment was recorded — `analyze_transcript()`
uses the fallback initialization (prerequisite KCs at p=0.90, target KCs at p=0.10).

`evaluator_snapshot` is `null` for webapp sessions and populated for simulation JSONL
sessions. The analyzer uses it as a shortcut when available; otherwise replays BKT.

---

## Post-Hoc Analysis — Trigger and Retrieve

### Trigger analysis for a session

Runs `analyze_transcript()` in the background and stores the result.  Returns
immediately with `"analysis_status": "pending"`.

```bash
curl -s -X POST http://localhost:8000/api/export/sessions/<session_id>/analyze \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**Response:**
```json
{ "session_id": "...", "analysis_status": "pending" }
```

**Status flow:** `pending` → `running` → `ready` (or `failed` on error).

Re-triggering an already-`ready` session is allowed (re-runs the analysis).
Returns 409 if analysis is currently `running`.

### Retrieve analysis results

```bash
curl -s http://localhost:8000/api/export/sessions/<session_id>/analysis \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**Response when ready:**
```json
{
  "session_id": "...",
  "analysis_status": "ready",
  "analysis": {
    "session_id": "...",
    "article_id": "...",
    "nac": 0.95,
    "kft": 0.72,
    "pr": 0.88,
    "lcq": 0.61,
    "mrq": 1.0,
    "mrq_adjustment": 0.075,
    "composite": 0.74,
    "total_tutor_turns": 18,
    "is_valid": true,
    "reviewer_active": true,
    "reviewer_rewrite_count": 2,
    "turn_results": [ ... ]
  }
}
```

**Response while pending/running:**
```json
{ "session_id": "...", "analysis_status": "running", "analysis": null }
```

### Offline CLI (no server required)

```bash
# Fetch analysis input first
curl -s http://localhost:8000/api/admin/sessions/<session_id>/analysis-input \
  -H "Authorization: Bearer $TOKEN" > session_input.json

# Score locally
python score.py session_input.json

# Disable NAC (faster, skips per-turn Haiku calls)
python score.py session_input.json --no-nac --output result.json
```

---

## Troubleshooting

### 401 Unauthorized
You are either not passing a token, or passing the wrong token (e.g. Anthropic API key instead of webapp JWT). Get a fresh token via `/api/auth/login`.

### 403 Forbidden
Your account is not a superuser. Grant it in the DB:
```bash
sqlite3 /path/to/webapp.db "UPDATE users SET is_superuser=1 WHERE email='you@example.com';"
```

### 402 Payment Required
Your account has 0 credits. Add credits via the admin endpoint (superusers are exempt from credit checks).

### domain_map_status stays "pending" forever
The background task likely failed silently due to a missing `ANTHROPIC_API_KEY`. Check:
1. In the terminal where the server runs: `echo $ANTHROPIC_API_KEY`
2. If blank, stop the server, run `export ANTHROPIC_API_KEY=sk-ant-...`, then restart
3. The server must be started *after* the export — environment variables are not inherited by already-running processes

### domain_map_status is "ready" but kc_count is 0
The topic is too broad for the domain mapper to extract discrete knowledge components. Delete the article and retry with a narrower subtopic:
```bash
sqlite3 /path/to/webapp.db "DELETE FROM articles WHERE id='<article_id>';"
```
Then re-resolve with a more specific Wikipedia URL (e.g. "Cyclic group" instead of "Group theory").

### Retrying a failed domain map
The `/resolve` endpoint skips background task generation if `domain_map_status = "ready"`. To force a rebuild, reset the status first:
```bash
sqlite3 /path/to/webapp.db \
  "UPDATE articles SET domain_map=NULL, domain_map_status='pending' WHERE id='<article_id>';"
```
Then re-POST to `/api/articles/resolve`.
