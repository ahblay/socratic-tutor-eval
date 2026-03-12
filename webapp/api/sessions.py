"""
webapp/api/sessions.py

Session lifecycle: create, turn-by-turn conversation, end.
Stub implementation — hot path (POST /turn) is Phase 3.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webapp.api.auth import get_current_user
from webapp.db import get_db
from webapp.db.models import Article, Session, Turn, User

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
    bkt_snapshot: dict
    frontier: list[str]
    turn_number: int


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

    session = Session(
        user_id=user.id,
        article_id=article.id,
        status="pre_assessment",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    return SessionResponse(
        session_id=session.id,
        article_id=session.article_id,
        status=session.status,
        turn_count=session.turn_count,
    )


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    session = await _get_session_or_404(session_id, user.id, db)
    return SessionResponse(
        session_id=session.id,
        article_id=session.article_id,
        status=session.status,
        turn_count=session.turn_count,
    )


@router.post("/{session_id}/turn", response_model=TurnResponse)
async def post_turn(
    session_id: str,
    body: TurnRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Main conversation turn.  Full implementation in Phase 3.
    """
    session = await _get_session_or_404(session_id, user.id, db)
    if session.status not in ("active",):
        raise HTTPException(
            status_code=409,
            detail=f"Session is not active (status: {session.status})",
        )

    # TODO (Phase 3): reconstruct SocraticTutor from tutor_state_snapshot,
    # call respond(), run BKTEvaluator, persist states, serialize tutor state.
    raise HTTPException(status_code=501, detail="Turn processing not yet implemented")


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
