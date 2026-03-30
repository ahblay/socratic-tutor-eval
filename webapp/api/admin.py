"""
webapp/api/admin.py

Superuser-only administration endpoints.

All handlers check is_superuser on the authenticated user and return 403
for anyone else.  No middleware — the check lives in each handler so the
FastAPI dependency graph stays simple.

Endpoints
---------
POST /api/admin/users/{user_id}/credits              — add credits to a user
POST /api/admin/articles/{article_id}/publish        — make article visible in catalog
POST /api/admin/articles/{article_id}/unpublish
GET  /api/admin/users                                — list all users with credit balances
GET  /api/admin/users/{user_id}/sessions             — list all sessions for a user
GET  /api/admin/sessions/{session_id}/transcript     — full transcript (stripped)
GET  /api/admin/sessions/{session_id}/guardrail-review — transcript with NAC metadata
GET  /api/admin/sessions/{session_id}/analysis-input — everything needed for analyze_transcript()
GET  /api/admin/sessions/{session_id}/analysis-view  — merged analysis + dialogue for viewer
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webapp.api.auth import get_current_user
from webapp.db import get_db
from webapp.db.models import Article, Assessment, BKTStateRow, Session, Turn, User

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
                "reviewer_verdict": t.reviewer_verdict if t.role == "tutor" else None,
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


# ---------------------------------------------------------------------------
# Analysis input
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}/analysis-input")
async def get_analysis_input(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return everything needed to run analyze_transcript() on a session:

    - domain_map:          KC graph from the article
    - bkt_initial_states:  per-KC p_mastered values written after assessment completes;
                           empty dict when no assessment was recorded (analyzer uses fallback)
    - assessment_turns:    ordered assessment Q&A pairs with observation_class
    - lesson_turns:        ordered lesson turns with raw_content, reviewer_verdict,
                           tutor_state_snapshot, and evaluator_snapshot (null for webapp
                           sessions; populated in simulation logs)
    """
    _require_superuser(current_user)

    result = await db.execute(
        select(Session, Article)
        .join(Article, Session.article_id == Article.id)
        .where(Session.id == session_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    session, article = row

    if article.domain_map is None:
        raise HTTPException(
            status_code=409,
            detail="Domain map not ready for this article",
        )

    # BKT initial states — written to bkt_states after assessment completes.
    # Keyed by (user_id, article_id), so they persist across sessions on the same article.
    bkt_result = await db.execute(
        select(BKTStateRow)
        .where(BKTStateRow.user_id == session.user_id)
        .where(BKTStateRow.article_id == session.article_id)
    )
    bkt_initial_states = {
        row.kc_id: {
            "p_mastered": row.p_mastered,
            "knowledge_class": row.knowledge_class,
            "observation_history": row.observation_history,
        }
        for row in bkt_result.scalars().all()
    }

    # Assessment turns — only rows where the student actually answered
    assessment_result = await db.execute(
        select(Assessment)
        .where(Assessment.session_id == session_id)
        .where(Assessment.user_answer.is_not(None))
        .order_by(Assessment.question_index)
    )
    assessment_turns = [
        {
            "question_index": a.question_index,
            "kc_id": a.kc_id,
            "question_text": a.question_text,
            "user_answer": a.user_answer,
            "observation_class": a.observation_class,
        }
        for a in assessment_result.scalars().all()
    ]

    # Lesson turns — full metadata for evaluation
    turns_result = await db.execute(
        select(Turn)
        .where(Turn.session_id == session_id)
        .order_by(Turn.turn_number)
    )
    lesson_turns = [
        {
            "turn_number": t.turn_number,
            "role": t.role,
            "content": t.content,
            "raw_content": t.raw_content,
            "reviewer_verdict": t.reviewer_verdict,
            "tutor_state_snapshot": t.tutor_state_snapshot,
            "evaluator_snapshot": t.evaluator_snapshot,
        }
        for t in turns_result.scalars().all()
    ]

    return {
        "session_id": session_id,
        "article_id": session.article_id,
        "article_title": article.canonical_title,
        "domain_map": article.domain_map,
        "bkt_initial_states": bkt_initial_states,
        "assessment_turns": assessment_turns,
        "lesson_turns": lesson_turns,
    }


@router.get("/sessions/{session_id}/analysis-view")
async def get_analysis_view(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return merged analysis + dialogue data for the analysis viewer.

    Combines sessions.analysis (EvaluationResult) with Turn dialogue text from
    the DB.  One frame per TurnResult (tutor turns only), in session order.

    - 404 if session not found
    - 409 if analysis is not ready (status != "ready" or null)
    - 200 {session_id, article_title, domain_map, metrics, frames}
    """
    _require_superuser(current_user)

    result = await db.execute(
        select(Session, Article)
        .join(Article, Session.article_id == Article.id)
        .where(Session.id == session_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    session, article = row

    if session.analysis is None or session.analysis_status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"Analysis not ready (status: {session.analysis_status or 'none'})",
        )

    analysis = session.analysis  # dict from EvaluationResult.to_dict()

    turns_result = await db.execute(
        select(Turn)
        .where(Turn.session_id == session_id)
        .order_by(Turn.turn_number)
    )
    all_turns = turns_result.scalars().all()
    turns_by_number = {t.turn_number: t for t in all_turns}

    frames = []
    for tr in analysis.get("turn_results", []):
        tn = tr.get("turn_number")
        tutor_turn = turns_by_number.get(tn)

        # Find the last user turn before this tutor turn
        student_message = None
        for t in reversed(all_turns):
            if t.turn_number < tn and t.role == "user":
                student_message = t.content
                break

        frames.append({
            "turn_number":            tn,
            "targeted_kc_id":         tr.get("targeted_kc_id"),
            "kc_status":              tr.get("kc_status"),
            "nac_verdict":            tr.get("nac_verdict"),
            "reviewer_verdict":       tr.get("reviewer_verdict"),
            "observed_type":          tr.get("observed_type"),
            "warranted_type":         tr.get("warranted_type"),
            "mrq_verdict":            tr.get("mrq_verdict"),
            "bkt_snapshot":           tr.get("bkt_snapshot", {}),
            "preceding_observations": tr.get("preceding_observations", []),
            "is_stall_turn":          tr.get("is_stall_turn", False),
            "stall_shape":            tr.get("stall_shape"),
            "student_message":        student_message,
            "tutor_response":         tutor_turn.content if tutor_turn else None,
        })

    metrics_keys = [
        "nac", "kft", "pr", "lcq", "mrq", "mrq_adjustment", "composite",
        "total_tutor_turns", "is_valid", "invalidity_reason",
        "reviewer_active", "reviewer_rewrite_count",
    ]
    metrics = {k: analysis.get(k) for k in metrics_keys}

    return {
        "session_id":    session_id,
        "article_title": article.canonical_title,
        "domain_map":    article.domain_map,
        "metrics":       metrics,
        "frames":        frames,
    }
