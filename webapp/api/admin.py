"""
webapp/api/admin.py

Superuser-only administration endpoints.

All handlers check is_superuser on the authenticated user and return 403
for anyone else.  No middleware — the check lives in each handler so the
FastAPI dependency graph stays simple.

Endpoints
---------
POST /api/admin/users/{user_id}/credits       — add credits to a user
POST /api/admin/articles/{article_id}/publish — make article visible in catalog
POST /api/admin/articles/{article_id}/unpublish
GET  /api/admin/users                         — list all users with credit balances
GET  /api/admin/users/{user_id}/sessions      — list all sessions for a user
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webapp.api.auth import get_current_user
from webapp.db import get_db
from webapp.db.models import Article, Assessment, Session, Turn, User

router = APIRouter()


def _require_superuser(user: User) -> None:
    if not user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser access required")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AddCreditsRequest(BaseModel):
    amount: int = Field(..., gt=0, description="Number of credits to add (must be positive)")


class UserSummary(BaseModel):
    user_id: str
    email: str | None
    is_anonymous: bool
    is_superuser: bool
    credits_remaining: int


# ---------------------------------------------------------------------------
# User endpoints
# ---------------------------------------------------------------------------

@router.get("/users", response_model=list[UserSummary])
async def list_users(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return all users with their current credit balances."""
    _require_superuser(current_user)
    result = await db.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()
    return [
        UserSummary(
            user_id=u.id,
            email=u.email,
            is_anonymous=u.is_anonymous,
            is_superuser=u.is_superuser,
            credits_remaining=u.credits_remaining,
        )
        for u in users
    ]


@router.post("/users/{user_id}/credits")
async def add_credits(
    user_id: str,
    body: AddCreditsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add credits to a user's balance."""
    _require_superuser(current_user)

    result = await db.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    target.credits_remaining += body.amount
    await db.commit()
    return {"user_id": user_id, "credits_remaining": target.credits_remaining}


@router.get("/users/{user_id}/sessions")
async def list_user_sessions(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all sessions for a user with article title, status, and turn count."""
    _require_superuser(current_user)

    result = await db.execute(select(User).where(User.id == user_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="User not found")

    sessions_result = await db.execute(
        select(Session, Article)
        .join(Article, Session.article_id == Article.id)
        .where(Session.user_id == user_id)
        .order_by(Session.started_at)
    )
    rows = sessions_result.all()
    return [
        {
            "session_id": s.id,
            "article_id": s.article_id,
            "article_title": a.canonical_title,
            "status": s.status,
            "turn_count": s.turn_count,
            "started_at": s.started_at.isoformat(),
        }
        for s, a in rows
    ]


@router.get("/sessions/{session_id}/transcript")
async def get_session_transcript(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the full transcript for any session (superuser only)."""
    _require_superuser(current_user)

    result = await db.execute(select(Session).where(Session.id == session_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    assessment_result = await db.execute(
        select(Assessment)
        .where(Assessment.session_id == session_id)
        .where(Assessment.user_answer.is_not(None))
        .order_by(Assessment.question_index)
    )
    assessments = assessment_result.scalars().all()

    turns_result = await db.execute(
        select(Turn)
        .where(Turn.session_id == session_id)
        .order_by(Turn.turn_number)
    )
    turns = turns_result.scalars().all()

    all_turns = []
    for a in assessments:
        all_turns.append({"role": "tutor", "content": a.question_text})
        all_turns.append({"role": "user",  "content": a.user_answer})
    for t in turns:
        all_turns.append({"role": t.role, "content": t.content})

    return {"session_id": session_id, "turns": all_turns}


@router.get("/sessions/{session_id}/guardrail-review")
async def get_guardrail_review(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return the full transcript with pre- and post-guardrail responses for every
    tutor turn. Useful for manually evaluating NAC (non-answer compliance).

    Each tutor turn includes:
      - content:        post-guardrail response shown to the student
      - raw_content:    pre-guardrail response from the tutor (may equal content)
      - guardrail_fired: true when the guardrail rewrote the response
    """
    _require_superuser(current_user)

    result = await db.execute(select(Session).where(Session.id == session_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    turns_result = await db.execute(
        select(Turn)
        .where(Turn.session_id == session_id)
        .order_by(Turn.turn_number)
    )
    turns = turns_result.scalars().all()

    return {
        "session_id": session_id,
        "turns": [
            {
                "turn_number": t.turn_number,
                "role": t.role,
                "content": t.content,
                "raw_content": t.raw_content if t.role == "tutor" else None,
                "guardrail_fired": (
                    t.raw_content is not None and t.raw_content != t.content
                    if t.role == "tutor" else None
                ),
            }
            for t in turns
        ],
    }


# ---------------------------------------------------------------------------
# Article endpoints
# ---------------------------------------------------------------------------

@router.post("/articles/{article_id}/publish")
async def publish_article(
    article_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Make an article visible in the lesson catalog."""
    _require_superuser(current_user)

    result = await db.execute(select(Article).where(Article.id == article_id))
    article = result.scalar_one_or_none()
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    if article.domain_map_status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Domain map not ready (status: {article.domain_map_status}); cannot publish",
        )

    article.is_published = True
    await db.commit()
    return {"article_id": article_id, "is_published": True}


@router.post("/articles/{article_id}/unpublish")
async def unpublish_article(
    article_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove an article from the lesson catalog."""
    _require_superuser(current_user)

    result = await db.execute(select(Article).where(Article.id == article_id))
    article = result.scalar_one_or_none()
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")

    article.is_published = False
    await db.commit()
    return {"article_id": article_id, "is_published": False}
