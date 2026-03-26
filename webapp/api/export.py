"""
webapp/api/export.py

Dataset export (JSONL) + post-hoc transcript analysis.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webapp.api.auth import get_current_user
from webapp.db import get_db
from webapp.db.models import Article, Assessment, BKTStateRow, Session, Turn, User

router = APIRouter()


def _require_superuser(user: User) -> None:
    if not user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser access required")


@router.get("/sessions")
async def export_sessions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Export all sessions as JSONL (admin)."""
    _require_superuser(user)
    raise HTTPException(status_code=501, detail="Export not yet implemented")


@router.get("/sessions/{session_id}")
async def export_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Export a single session as JSONL."""
    _require_superuser(user)
    raise HTTPException(status_code=501, detail="Export not yet implemented")


@router.post("/sessions/{session_id}/analyze")
async def trigger_analysis(
    session_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Trigger post-hoc analysis of a completed session.

    Runs analyze_transcript() in the background and stores results in
    session.analysis / session.analysis_status.  Returns immediately with
    status "pending".

    - 404 if session not found
    - 409 if analysis is already running, or if the article has no domain map
    - 200 {"session_id": ..., "analysis_status": "pending"} otherwise
      (re-triggering a completed analysis is allowed)
    """
    _require_superuser(user)

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

    if session.analysis_status == "running":
        raise HTTPException(
            status_code=409,
            detail="Analysis already in progress",
        )

    session.analysis_status = "pending"
    await db.commit()

    background_tasks.add_task(_run_analysis_bg, session_id=session_id)
    return {"session_id": session_id, "analysis_status": "pending"}


@router.get("/sessions/{session_id}/analysis")
async def get_analysis(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Return analysis results for a session.

    - 404 if session not found
    - 200 with analysis_status and analysis (null when not ready)
    """
    _require_superuser(user)

    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session_id,
        "analysis_status": session.analysis_status,
        "analysis": session.analysis if session.analysis_status == "ready" else None,
    }


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

async def _run_analysis_bg(session_id: str) -> None:
    """
    Fetch session data, run analyze_transcript() in a thread, and store result.

    Uses its own AsyncSessionLocal because the request session is already
    closed when background tasks execute.  analyze_transcript() calls the
    synchronous Anthropic SDK, so we offload it to a thread via
    asyncio.to_thread().
    """
    from sqlalchemy import select as _select

    from webapp.db import AsyncSessionLocal
    from webapp.db.models import (
        Article as _Article,
        Assessment as _Assessment,
        BKTStateRow as _BKTStateRow,
        Session as _Session,
        Turn as _Turn,
    )
    from tutor_eval.evaluation.analyzer import analyze_transcript

    # ── Step 1: load data and mark as running ──────────────────────────────
    async with AsyncSessionLocal() as db:
        fetch = await db.execute(
            _select(_Session, _Article)
            .join(_Article, _Session.article_id == _Article.id)
            .where(_Session.id == session_id)
        )
        row = fetch.one_or_none()
        if row is None:
            return
        session, article = row

        if article.domain_map is None:
            session.analysis_status = "failed"
            await db.commit()
            return

        bkt_fetch = await db.execute(
            _select(_BKTStateRow)
            .where(_BKTStateRow.user_id == session.user_id)
            .where(_BKTStateRow.article_id == session.article_id)
        )
        bkt_initial_states = {
            r.kc_id: {
                "p_mastered": r.p_mastered,
                "knowledge_class": r.knowledge_class,
                "observation_history": r.observation_history,
            }
            for r in bkt_fetch.scalars().all()
        }

        assessment_fetch = await db.execute(
            _select(_Assessment)
            .where(_Assessment.session_id == session_id)
            .where(_Assessment.user_answer.is_not(None))
            .order_by(_Assessment.question_index)
        )
        assessment_turns = [
            {
                "question_index": a.question_index,
                "kc_id": a.kc_id,
                "question_text": a.question_text,
                "user_answer": a.user_answer,
                "observation_class": a.observation_class,
            }
            for a in assessment_fetch.scalars().all()
        ]

        turns_fetch = await db.execute(
            _select(_Turn)
            .where(_Turn.session_id == session_id)
            .order_by(_Turn.turn_number)
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
            for t in turns_fetch.scalars().all()
        ]

        analysis_input = {
            "session_id": session_id,
            "article_id": session.article_id,
            "article_title": article.canonical_title,
            "domain_map": article.domain_map,
            "bkt_initial_states": bkt_initial_states,
            "assessment_turns": assessment_turns,
            "lesson_turns": lesson_turns,
        }

        session.analysis_status = "running"
        await db.commit()

    # ── Step 2: run analyze_transcript() in a thread ───────────────────────
    try:
        eval_result = await asyncio.to_thread(analyze_transcript, analysis_input)
    except Exception:
        async with AsyncSessionLocal() as db:
            fetch = await db.execute(_select(_Session).where(_Session.id == session_id))
            session = fetch.scalar_one_or_none()
            if session is not None:
                session.analysis_status = "failed"
                await db.commit()
        return

    # ── Step 3: persist result ─────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        fetch = await db.execute(_select(_Session).where(_Session.id == session_id))
        session = fetch.scalar_one_or_none()
        if session is None:
            return
        session.analysis = eval_result.to_dict()
        session.analysis_status = "ready"
        await db.commit()
