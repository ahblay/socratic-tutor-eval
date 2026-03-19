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
    classify_opener_answer,
    classify_full_assessment,
    generate_followup_question,
    class_from_l0,
    propagate_l0,
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
    observation_class: str | None = None


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

    # Classify the opener eagerly — used as global_prior in complete_assessment
    observation_class: str | None = None
    if current_row.question_index == 0:
        observation_class = await classify_opener_answer(
            student_response=body.answer,
            topic=article.canonical_title,
            client=client,
        )

    # Save the answer
    current_row.user_answer = body.answer
    current_row.observation_class = observation_class
    current_row.completed_at = datetime.now(timezone.utc)

    # Count answered follow-ups (including the one just saved)
    completed_followups = sum(
        1 for r in rows if r.question_index >= 1 and r.user_answer is not None
    )
    if current_row.question_index >= 1:
        completed_followups += 1

    if completed_followups < MAX_FOLLOWUPS:
        # Build conversation so far for context
        answered = sorted(
            [r for r in rows if r.user_answer is not None] + [current_row],
            key=lambda r: r.question_index,
        )
        # Deduplicate (current_row may already be in rows if re-queried)
        seen = set()
        conversation: list[dict] = []
        for r in answered:
            if r.question_index not in seen:
                seen.add(r.question_index)
                conversation.append({"role": "tutor",   "text": r.question_text})
                conversation.append({"role": "student", "text": r.user_answer})

        next_question = await generate_followup_question(
            conversation=conversation,
            topic=article.canonical_title,
            domain_map=article.domain_map or {},
            client=client,
        )
        next_row = Assessment(
            session_id=session_id,
            question_index=current_row.question_index + 1,
            kc_id=f"__followup_{completed_followups}__",
            question_text=next_question,
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
    domain_map = article.domain_map or {}

    # Build full conversation for holistic classification
    answered = sorted(
        [r for r in rows if r.user_answer is not None],
        key=lambda r: r.question_index,
    )
    conversation: list[dict] = []
    for r in answered:
        conversation.append({"role": "tutor",   "text": r.question_text})
        conversation.append({"role": "student", "text": r.user_answer})

    client = anthropic.AsyncAnthropic()
    assessed_kcs = await classify_full_assessment(
        conversation=conversation,
        topic=article.canonical_title,
        domain_map=domain_map,
        client=client,
    )

    # Propagate L0 across the full KC graph
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


