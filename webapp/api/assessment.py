"""
webapp/api/assessment.py

Pre-session assessment: 1 opener question + up to 3 targeted follow-ups.
Initializes BKT L0 values and transitions session to active.
"""

from __future__ import annotations

from datetime import datetime, timezone

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webapp.api.auth import get_current_user
from webapp.api.sessions import _get_session_or_404
from webapp.db import get_db
from webapp.db.models import Article, Assessment, BKTStateRow, Session, User
from webapp.services.assessment_service import (
    OPENER_KC_ID,
    OPENER_TEXT,
    MAX_FOLLOWUPS,
    classify_assessment_answer,
    classify_opener_answer,
    class_from_l0,
    kc_description_for,
    propagate_l0,
    select_followup_kcs,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class StartAssessmentResponse(BaseModel):
    question_index: int
    question_text: str
    kc_id: str
    assessment_complete: bool


class AnswerRequest(BaseModel):
    answer: str


class AnswerAssessmentResponse(BaseModel):
    question_index: int
    question_text: str | None
    kc_id: str | None
    assessment_complete: bool
    observation_class: str


class CompleteAssessmentResponse(BaseModel):
    session_id: str
    status: str
    bkt_initialized: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/{session_id}/assessment/start", response_model=StartAssessmentResponse)
async def start_assessment(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StartAssessmentResponse:
    """
    Generate the fixed opener question and pre-compute the follow-up KC queue.
    Idempotent — safe to call twice after a network failure.
    """
    session = await _get_session_or_404(session_id, user.id, db)
    if session.status != "pre_assessment":
        raise HTTPException(
            status_code=409,
            detail=f"Session is not in pre_assessment (status: {session.status})",
        )

    # Load article
    article_result = await db.execute(
        select(Article).where(Article.id == session.article_id)
    )
    article = article_result.scalar_one()

    opener_text = OPENER_TEXT.format(topic=article.canonical_title)

    # Idempotent: return existing opener row if already created
    existing_result = await db.execute(
        select(Assessment)
        .where(Assessment.session_id == session_id)
        .where(Assessment.question_index == 0)
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        return StartAssessmentResponse(
            question_index=0,
            question_text=existing.question_text,
            kc_id=OPENER_KC_ID,
            assessment_complete=False,
        )

    # Pre-compute follow-up queue and store in session state
    followup_queue = select_followup_kcs(article.domain_map or {}, MAX_FOLLOWUPS)
    session.tutor_state_snapshot = {"assessment_queue": followup_queue}

    # Create opener Assessment row
    opener_row = Assessment(
        session_id=session_id,
        question_index=0,
        kc_id=OPENER_KC_ID,
        question_text=opener_text,
    )
    db.add(opener_row)
    await db.commit()

    return StartAssessmentResponse(
        question_index=0,
        question_text=opener_text,
        kc_id=OPENER_KC_ID,
        assessment_complete=False,
    )


@router.post("/{session_id}/assessment/answer", response_model=AnswerAssessmentResponse)
async def answer_question(
    session_id: str,
    body: AnswerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AnswerAssessmentResponse:
    """
    Submit an answer to the current open question. Classifies the response,
    saves it, and either returns the next question or signals completion.
    """
    session = await _get_session_or_404(session_id, user.id, db)
    if session.status != "pre_assessment":
        raise HTTPException(
            status_code=409,
            detail=f"Session is not in pre_assessment (status: {session.status})",
        )

    article_result = await db.execute(
        select(Article).where(Article.id == session.article_id)
    )
    article = article_result.scalar_one()

    # Load all assessment rows ordered by index
    rows_result = await db.execute(
        select(Assessment)
        .where(Assessment.session_id == session_id)
        .order_by(Assessment.question_index)
    )
    rows = list(rows_result.scalars().all())

    if not rows:
        raise HTTPException(status_code=409, detail="Assessment not started — call /start first")

    # Find the current unanswered row (highest index with no answer)
    current_row = next((r for r in rows if r.user_answer is None), None)
    if current_row is None:
        raise HTTPException(status_code=409, detail="No open question to answer")

    client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env

    # Classify the student's answer
    if current_row.question_index == 0:
        # Opener: classify overall prior knowledge
        observation_class = await classify_opener_answer(
            student_response=body.answer,
            topic=article.canonical_title,
            client=client,
        )
    else:
        # Follow-up: classify KC-specific knowledge
        kc_name = _kc_name_for_id(current_row.kc_id, article.domain_map or {})
        kc_description = kc_description_for(kc_name, article.domain_map or {})
        observation_class = await classify_assessment_answer(
            student_response=body.answer,
            kc_name=kc_name,
            kc_description=kc_description,
            client=client,
        )

    # Save the answer
    current_row.user_answer = body.answer
    current_row.observation_class = observation_class
    current_row.completed_at = datetime.now(timezone.utc)

    # After opener: trim queue to 1 if student is mastered (short-circuit)
    assessment_queue: list[dict] = (session.tutor_state_snapshot or {}).get("assessment_queue", [])
    if current_row.question_index == 0 and observation_class == "mastered":
        assessment_queue = assessment_queue[:1]
        session.tutor_state_snapshot = {
            **(session.tutor_state_snapshot or {}),
            "assessment_queue": assessment_queue,
        }

    # Count completed follow-ups (index >= 1, already answered)
    completed_followups = sum(
        1 for r in rows if r.question_index >= 1 and r.user_answer is not None
    )
    # Include the one we just answered if it's a follow-up
    if current_row.question_index >= 1:
        completed_followups += 1

    # Decide whether to issue a follow-up or signal completion
    next_queue_index = completed_followups  # 0-based into assessment_queue
    if next_queue_index < len(assessment_queue):
        next_kc = assessment_queue[next_queue_index]
        next_row = Assessment(
            session_id=session_id,
            question_index=current_row.question_index + 1,
            kc_id=next_kc["kc_id"],
            question_text=next_kc["question_text"],
        )
        db.add(next_row)
        await db.commit()

        return AnswerAssessmentResponse(
            question_index=next_row.question_index,
            question_text=next_row.question_text,
            kc_id=next_row.kc_id,
            assessment_complete=False,
            observation_class=observation_class,
        )
    else:
        # No more follow-ups — signal completion
        await db.commit()
        return AnswerAssessmentResponse(
            question_index=current_row.question_index,
            question_text=None,
            kc_id=None,
            assessment_complete=True,
            observation_class=observation_class,
        )


@router.post("/{session_id}/assessment/complete", response_model=CompleteAssessmentResponse)
async def complete_assessment(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CompleteAssessmentResponse:
    """
    Finalize the assessment: propagate L0 values, write BKTStateRows for all
    KCs in the domain map, and transition the session to active.
    """
    session = await _get_session_or_404(session_id, user.id, db)
    if session.status != "pre_assessment":
        raise HTTPException(
            status_code=409,
            detail=f"Session is not in pre_assessment (status: {session.status})",
        )

    article_result = await db.execute(
        select(Article).where(Article.id == session.article_id)
    )
    article = article_result.scalar_one()

    rows_result = await db.execute(
        select(Assessment)
        .where(Assessment.session_id == session_id)
        .order_by(Assessment.question_index)
    )
    rows = list(rows_result.scalars().all())

    # Must have at least an answered opener
    opener = next((r for r in rows if r.question_index == 0), None)
    if opener is None or opener.user_answer is None:
        raise HTTPException(status_code=409, detail="Assessment not started or opener not answered")

    global_prior = opener.observation_class or "absent"

    # Collect follow-up classifications
    assessed_kcs: dict[str, str] = {
        r.kc_id: r.observation_class
        for r in rows
        if r.question_index >= 1 and r.observation_class is not None
    }

    # Propagate L0 across the full KC graph
    domain_map = article.domain_map or {}
    l0_map = propagate_l0(domain_map, assessed_kcs, global_prior)

    # Upsert BKTStateRow for every KC
    now = datetime.now(timezone.utc)
    for kc_id, l0_value in l0_map.items():
        existing_result = await db.execute(
            select(BKTStateRow).where(
                BKTStateRow.user_id == user.id,
                BKTStateRow.article_id == session.article_id,
                BKTStateRow.kc_id == kc_id,
            )
        )
        existing = existing_result.scalar_one_or_none()
        if existing is None:
            db.add(BKTStateRow(
                user_id=user.id,
                article_id=session.article_id,
                kc_id=kc_id,
                p_mastered=l0_value,
                knowledge_class=class_from_l0(l0_value),
                observation_history=[],
            ))
        else:
            existing.p_mastered = l0_value
            existing.knowledge_class = class_from_l0(l0_value)
            existing.last_updated = now

    # Transition session to active
    session.status = "active"
    await db.commit()

    return CompleteAssessmentResponse(
        session_id=session_id,
        status="active",
        bkt_initialized=len(l0_map),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _kc_name_for_id(kc_id: str, domain_map: dict) -> str:
    """Reverse-lookup: kc_id slug → concept name from domain map."""
    from webapp.services.domain_cache import _slugify
    for c in domain_map.get("core_concepts", []):
        name = c.get("concept", "")
        if _slugify(name) == kc_id:
            return name
    return kc_id  # fallback: return the slug itself
