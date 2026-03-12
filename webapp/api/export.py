"""
webapp/api/export.py

Dataset export (JSONL) + post-hoc transcript analysis.
Full implementation in Phase 8.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from webapp.api.auth import get_current_user
from webapp.db import get_db
from webapp.db.models import User

router = APIRouter()


@router.get("/sessions")
async def export_sessions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Export all sessions as JSONL (admin)."""
    raise HTTPException(status_code=501, detail="Export not yet implemented")


@router.get("/sessions/{session_id}")
async def export_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Export a single session as JSONL."""
    raise HTTPException(status_code=501, detail="Export not yet implemented")


@router.post("/sessions/{session_id}/analyze")
async def trigger_analysis(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Trigger post-hoc BKT analysis of a completed session."""
    raise HTTPException(status_code=501, detail="Analysis not yet implemented")


@router.get("/sessions/{session_id}/analysis")
async def get_analysis(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return analysis results for a session."""
    raise HTTPException(status_code=501, detail="Analysis not yet implemented")
