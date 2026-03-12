"""
webapp/api/assessment.py

Pre-session 3-question assessment flow.
Full implementation in Phase 4.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webapp.api.auth import get_current_user
from webapp.db import get_db
from webapp.db.models import Session, User

router = APIRouter()


class AnswerRequest(BaseModel):
    question_index: int
    answer: str


@router.post("/{session_id}/assessment/start")
async def start_assessment(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate 3 checkpoint questions from the article's domain map."""
    # TODO (Phase 4): generate questions, create Assessment rows, return them
    raise HTTPException(status_code=501, detail="Assessment not yet implemented")


@router.post("/{session_id}/assessment/answer")
async def answer_question(
    session_id: str,
    body: AnswerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Score one assessment answer and update the Assessment row."""
    # TODO (Phase 4): classify answer, update Assessment row
    raise HTTPException(status_code=501, detail="Assessment not yet implemented")


@router.post("/{session_id}/assessment/complete")
async def complete_assessment(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Initialize BKT states from assessment results and transition session to active.
    """
    # TODO (Phase 4): initialize BKT, set session.status = "active"
    raise HTTPException(status_code=501, detail="Assessment not yet implemented")
