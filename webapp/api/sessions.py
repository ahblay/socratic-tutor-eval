"""
webapp/api/sessions.py

Session lifecycle: create, turn-by-turn conversation, end.
Stub implementation — hot path (POST /turn) is Phase 3.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webapp.api.auth import get_current_user
from webapp.db import get_db
from webapp.db.models import Article, Assessment, BKTStateRow, Session, Turn, User

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    article_id: str


class SessionResponse(BaseModel):
    session_id: str
    article_id: str
    status: str
    turn_count: int


class TurnRequest(BaseModel):
    message: str


class TurnResponse(BaseModel):
    reply: str
    turn_number: int
    tutor_state: dict | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("", response_model=SessionResponse)
async def create_session(
    body: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Article).where(Article.id == body.article_id)
    )
    article = result.scalar_one_or_none()
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    if article.domain_map_status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Domain map not ready (status: {article.domain_map_status})",
        )

    # Resume existing non-completed session if one exists
    existing_result = await db.execute(
        select(Session).where(
            Session.user_id == user.id,
            Session.article_id == body.article_id,
            Session.status.in_(["pre_assessment", "active"]),
        ).order_by(Session.started_at.desc()).limit(1)
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        return _session_response(existing)

    # Skip assessment if BKT state already exists for this user+article
    bkt_result = await db.execute(
        select(BKTStateRow).where(
            BKTStateRow.user_id == user.id,
            BKTStateRow.article_id == body.article_id,
        ).limit(1)
    )
    already_assessed = bkt_result.scalar_one_or_none() is not None

    session = Session(
        user_id=user.id,
        article_id=article.id,
        status="active" if already_assessed else "pre_assessment",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    return _session_response(session)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    session = await _get_session_or_404(session_id, user.id, db)
    return _session_response(session)


@router.post("/{session_id}/open", response_model=TurnResponse)
async def open_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Generate and persist the tutor's opening question (no student turn).
    Called once immediately after assessment completes. Idempotent.
    """
    from tutor_eval.tutors.socratic import SocraticTutor

    session = await _get_session_or_404(session_id, user.id, db)
    if session.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Session is not active (status: {session.status})",
        )

    # Idempotent: return existing opener if turns already exist
    turns_result = await db.execute(
        select(Turn).where(Turn.session_id == session_id).order_by(Turn.turn_number)
    )
    existing_turns = turns_result.scalars().all()
    if existing_turns:
        first_tutor = next((t for t in existing_turns if t.role == "tutor"), None)
        if first_tutor:
            return TurnResponse(reply=first_tutor.content, turn_number=session.turn_count)

    article_result = await db.execute(
        select(Article).where(Article.id == session.article_id)
    )
    article = article_result.scalar_one()

    tutor = SocraticTutor(
        topic=article.canonical_title,
        domain_map=article.domain_map,
        state=None,  # always fresh — no prior tutor state at session open
    )

    # Build history: assessment Q&A (context) + synthetic opener trigger.
    # The synthetic entry is never stored; it ensures the API receives a user-final message.
    assessment_hist = await _assessment_history(session_id, db)
    opener_history = assessment_hist + [
        {"role": "student", "text": "Please introduce the topic and ask your first Socratic question."}
    ]
    reply = await asyncio.to_thread(tutor.respond, "", opener_history)

    raw_reply = tutor._last_raw_response or reply
    tutor_state = tutor.session_state()
    usage = tutor._last_usage or {}

    db.add(Turn(
        session_id=session_id,
        turn_number=1,
        role="tutor",
        content=reply,
        raw_content=raw_reply,
        tutor_state_snapshot=tutor_state,
    ))

    session.tutor_state_snapshot = tutor_state
    session.total_input_tokens += usage.get("input_tokens", 0)
    session.total_output_tokens += usage.get("output_tokens", 0)
    # turn_count tracks student-initiated exchanges; opener does not consume a turn slot

    await db.commit()
    return TurnResponse(reply=reply, turn_number=session.turn_count, tutor_state=tutor_state)


@router.post("/{session_id}/turn", response_model=TurnResponse)
async def post_turn(
    session_id: str,
    body: TurnRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    One tutoring exchange:
      1. Validate session is active
      2. Load turn history from DB
      3. Reconstruct SocraticTutor from saved state
      4. Call respond() in a thread (sync SDK call)
      5. Persist student + tutor turns, update session state
    """
    from tutor_eval.tutors.socratic import SocraticTutor

    session = await _get_session_or_404(session_id, user.id, db)
    if session.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Session is not active (status: {session.status})",
        )

    # Load the article for domain_map and topic
    article_result = await db.execute(
        select(Article).where(Article.id == session.article_id)
    )
    article = article_result.scalar_one()

    # Load existing turns to build history for the tutor
    turns_result = await db.execute(
        select(Turn)
        .where(Turn.session_id == session_id)
        .order_by(Turn.turn_number)
    )
    existing_turns = turns_result.scalars().all()

    # Build history: assessment Q&A prefix + tutoring turns + current message
    assessment_hist = await _assessment_history(session_id, db)
    tutoring_hist = [
        {
            "role": "student" if t.role == "user" else "tutor",
            "text": t.content,
        }
        for t in existing_turns
    ]
    history = assessment_hist + tutoring_hist
    history.append({"role": "student", "text": body.message})

    # Reconstruct the tutor from saved state (None = fresh start)
    tutor = SocraticTutor(
        topic=article.canonical_title,
        domain_map=article.domain_map,
        state=session.tutor_state_snapshot,
    )

    # Call respond() in a thread pool (sync Anthropic SDK)
    reply = await asyncio.to_thread(tutor.respond, body.message, history)

    raw_reply = tutor._last_raw_response or reply
    tutor_state = tutor.session_state()
    usage = tutor._last_usage or {}

    # Next turn number (each exchange = 2 rows: user + tutor)
    next_turn_number = len(existing_turns) + 1

    # Persist student turn
    student_turn = Turn(
        session_id=session_id,
        turn_number=next_turn_number,
        role="user",
        content=body.message,
    )
    db.add(student_turn)

    # Persist tutor turn
    tutor_turn = Turn(
        session_id=session_id,
        turn_number=next_turn_number + 1,
        role="tutor",
        content=reply,
        raw_content=raw_reply,
        tutor_state_snapshot=tutor_state,
    )
    db.add(tutor_turn)

    # Update session
    session.turn_count += 1
    session.tutor_state_snapshot = tutor_state
    session.total_input_tokens += usage.get("input_tokens", 0)
    session.total_output_tokens += usage.get("output_tokens", 0)

    await db.commit()

    return TurnResponse(reply=reply, turn_number=session.turn_count, tutor_state=tutor_state)


@router.get("/{session_id}/graph-state")
async def get_graph_state(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return domain map + BKT snapshot for the knowledge graph panel."""
    session = await _get_session_or_404(session_id, user.id, db)

    article_result = await db.execute(
        select(Article).where(Article.id == session.article_id)
    )
    article = article_result.scalar_one()

    bkt_result = await db.execute(
        select(BKTStateRow).where(
            BKTStateRow.user_id == user.id,
            BKTStateRow.article_id == session.article_id,
        )
    )
    bkt_rows = bkt_result.scalars().all()
    bkt_snapshot = {row.kc_id: row.p_mastered for row in bkt_rows}

    return {
        "domain_map": article.domain_map,
        "bkt_snapshot": bkt_snapshot,
        "tutor_state": session.tutor_state_snapshot,
    }


@router.post("/{session_id}/end")
async def end_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from datetime import datetime, timezone
    session = await _get_session_or_404(session_id, user.id, db)
    session.status = "completed"
    session.ended_at = datetime.now(timezone.utc)
    await db.commit()
    return {"session_id": session_id, "status": "completed"}


@router.get("/{session_id}/transcript")
async def get_transcript(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    session = await _get_session_or_404(session_id, user.id, db)
    result = await db.execute(
        select(Turn)
        .where(Turn.session_id == session_id)
        .order_by(Turn.turn_number)
    )
    turns = result.scalars().all()
    return {
        "session_id": session_id,
        "turns": [
            {
                "turn_number": t.turn_number,
                "role": t.role,
                "content": t.content,
                "evaluator_snapshot": t.evaluator_snapshot,
            }
            for t in turns
        ],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_response(session: Session) -> SessionResponse:
    return SessionResponse(
        session_id=session.id,
        article_id=session.article_id,
        status=session.status,
        turn_count=session.turn_count,
    )


async def _get_session_or_404(
    session_id: str, user_id: str, db: AsyncSession
) -> Session:
    result = await db.execute(
        select(Session).where(
            Session.id == session_id,
            Session.user_id == user_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


async def _assessment_history(session_id: str, db: AsyncSession) -> list[dict]:
    """Return assessment Q&A as tutor/student history entries, ordered by question index."""
    result = await db.execute(
        select(Assessment)
        .where(
            Assessment.session_id == session_id,
            Assessment.user_answer.isnot(None),
        )
        .order_by(Assessment.question_index)
    )
    rows = result.scalars().all()
    history = []
    for row in rows:
        history.append({"role": "tutor",   "text": row.question_text})
        history.append({"role": "student", "text": row.user_answer})
    return history
