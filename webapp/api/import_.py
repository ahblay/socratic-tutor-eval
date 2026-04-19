"""
webapp/api/import_.py

Endpoint for importing locally-scored simulation transcripts into the webapp DB
so they can be viewed in the existing analysis viewer without re-running evaluation.

POST /api/import/sessions
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webapp.api.auth import get_current_user
from webapp.db import get_db
from webapp.db.models import Article, BKTStateRow, Session, Turn, User

router = APIRouter()


def _require_superuser(user: User) -> None:
    if not user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser access required")


def _synthetic_page_id(topic: str) -> int:
    digest = hashlib.sha256(topic.encode("utf-8")).digest()
    as_int = int.from_bytes(digest[:8], byteorder="big")
    bounded = as_int % (10 ** 15)
    return -bounded if bounded != 0 else -1


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ImportSessionRequest(BaseModel):
    transcript: dict
    result: dict
    article_id: str | None = None


class ImportSessionResponse(BaseModel):
    session_id: str
    article_id: str
    article_created: bool
    turn_count: int
    bkt_rows_written: int


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/sessions", response_model=ImportSessionResponse)
async def import_session(
    body: ImportSessionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Import a locally-scored simulation transcript into the webapp DB.

    The transcript's session_id is used directly as the DB session PK, so
    re-importing the same transcript returns 409. On success, the session is
    immediately viewable via GET /api/admin/sessions/{session_id}/analysis-view.
    """
    _require_superuser(current_user)

    # ------------------------------------------------------------------
    # Step 1 — Validate payload
    # ------------------------------------------------------------------
    transcript = body.transcript
    result = body.result

    session_id = transcript.get("session_id")
    if not session_id:
        raise HTTPException(status_code=422, detail="transcript.session_id is required")

    topic = transcript.get("topic")
    if not topic:
        raise HTTPException(status_code=422, detail="transcript.topic is required")

    turns_raw = transcript.get("turns")
    if not turns_raw or not isinstance(turns_raw, list):
        raise HTTPException(status_code=422, detail="transcript.turns must be a non-empty list")

    for i, turn in enumerate(turns_raw, start=1):
        role = turn.get("role")
        content = turn.get("content")
        if role not in ("student", "tutor"):
            raise HTTPException(status_code=422, detail=f"turn {i}: role must be 'student' or 'tutor'")
        if not content:
            raise HTTPException(status_code=422, detail=f"turn {i}: content must be non-empty")

    result_session_id = result.get("session_id")
    if not result_session_id:
        raise HTTPException(status_code=422, detail="result.session_id is required")

    if result_session_id != session_id:
        raise HTTPException(
            status_code=422,
            detail="result.session_id does not match transcript.session_id",
        )

    if "turn_results" not in result or not isinstance(result["turn_results"], list):
        raise HTTPException(status_code=422, detail="result.turn_results is required")

    bkt_initial_states_raw: dict = transcript.get("bkt_initial_states") or {}
    domain_map = transcript.get("domain_map")

    # ------------------------------------------------------------------
    # Step 2 — Collision check
    # ------------------------------------------------------------------
    existing = await db.execute(select(Session).where(Session.id == session_id))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"Session {session_id} already exists")

    # ------------------------------------------------------------------
    # Step 3 — Resolve or create Article
    # ------------------------------------------------------------------
    article_created = False

    if body.article_id is not None:
        art_result = await db.execute(select(Article).where(Article.id == body.article_id))
        article = art_result.scalar_one_or_none()
        if article is None:
            raise HTTPException(status_code=404, detail="Article not found")
    else:
        synthetic_id = _synthetic_page_id(topic)
        art_result = await db.execute(
            select(Article).where(Article.wikipedia_page_id == synthetic_id)
        )
        article = art_result.scalar_one_or_none()

        if article is None:
            article = Article(
                id=str(uuid.uuid4()),
                wikipedia_page_id=synthetic_id,
                canonical_title=topic,
                wikipedia_url="",
                summary=None,
                domain_map=domain_map,
                domain_map_status="ready" if domain_map is not None else "pending",
                is_published=False,
                last_fetched=datetime.now(timezone.utc),
            )
            db.add(article)
            await db.flush()
            article_created = True
        else:
            # Existing stub: verify it matches this topic (hash collision guard)
            if article.canonical_title != topic:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Synthetic wikipedia_page_id {synthetic_id} is already in use "
                        f"by article '{article.canonical_title}' (hash collision). "
                        "Provide article_id to link to an existing article instead."
                    ),
                )

    # ------------------------------------------------------------------
    # Step 4 — Create Session
    # ------------------------------------------------------------------
    raw_date = transcript.get("date")
    started_at = datetime.now(timezone.utc)
    if raw_date:
        try:
            parsed = datetime.strptime(raw_date, "%Y-%m-%d")
            started_at = parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass  # fall back to now()

    student_turn_count = sum(1 for t in turns_raw if t["role"] == "student")

    session = Session(
        id=session_id,
        user_id=current_user.id,
        article_id=article.id,
        started_at=started_at,
        ended_at=None,
        turn_count=student_turn_count,
        status="completed",
        analysis=result,
        analysis_status="ready",
        max_turns=None,
        total_input_tokens=0,
        total_output_tokens=0,
        tutor_state_snapshot=None,
    )
    db.add(session)
    await db.flush()

    # ------------------------------------------------------------------
    # Step 5 — Create Turn rows (1-based sequential, matching converter.py)
    # ------------------------------------------------------------------
    for index, raw_turn in enumerate(turns_raw, start=1):
        db_role = "user" if raw_turn["role"] == "student" else "tutor"
        db.add(Turn(
            id=str(uuid.uuid4()),
            session_id=session_id,
            turn_number=index,
            role=db_role,
            content=raw_turn["content"],
            raw_content=None,
            reviewer_verdict=None,
            tutor_state_snapshot=None,
            evaluator_snapshot=None,
        ))

    # ------------------------------------------------------------------
    # Step 6 — Create BKTStateRow rows (insert-if-absent)
    # ------------------------------------------------------------------
    bkt_rows_written = 0
    for kc_id, state in bkt_initial_states_raw.items():
        existing_bkt = await db.execute(
            select(BKTStateRow).where(
                BKTStateRow.user_id == current_user.id,
                BKTStateRow.article_id == article.id,
                BKTStateRow.kc_id == kc_id,
            )
        )
        if existing_bkt.scalar_one_or_none() is not None:
            continue  # do not overwrite existing BKT state

        db.add(BKTStateRow(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            article_id=article.id,
            kc_id=kc_id,
            p_mastered=state.get("p_mastered", 0.10),
            knowledge_class=state.get("knowledge_class", "absent"),
            observation_history=state.get("observation_history", []),
            last_updated=datetime.now(timezone.utc),
        ))
        bkt_rows_written += 1

    # ------------------------------------------------------------------
    # Step 7 — Commit
    # ------------------------------------------------------------------
    await db.commit()

    return ImportSessionResponse(
        session_id=session_id,
        article_id=article.id,
        article_created=article_created,
        turn_count=len(turns_raw),
        bkt_rows_written=bkt_rows_written,
    )
