"""
webapp/db/models.py

SQLAlchemy ORM models.  All seven tables from the implementation plan.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    email: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    hashed_password: Mapped[str | None] = mapped_column(String, nullable=True)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    sessions: Mapped[list[Session]] = relationship("Session", back_populates="user")
    bkt_states: Mapped[list[BKTStateRow]] = relationship("BKTStateRow", back_populates="user")
    retention_schedules: Mapped[list[RetentionSchedule]] = relationship(
        "RetentionSchedule", back_populates="user"
    )


# ---------------------------------------------------------------------------
# articles
# ---------------------------------------------------------------------------

class Article(Base):
    __tablename__ = "articles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    wikipedia_page_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    canonical_title: Mapped[str] = mapped_column(String, nullable=False)
    wikipedia_url: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    domain_map_version: Mapped[int] = mapped_column(Integer, default=1)
    # "pending" | "ready" | "failed"
    domain_map_status: Mapped[str] = mapped_column(String, default="pending")
    last_fetched: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    sessions: Mapped[list[Session]] = relationship("Session", back_populates="article")
    bkt_states: Mapped[list[BKTStateRow]] = relationship("BKTStateRow", back_populates="article")
    retention_schedules: Mapped[list[RetentionSchedule]] = relationship(
        "RetentionSchedule", back_populates="article"
    )


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    article_id: Mapped[str] = mapped_column(String, ForeignKey("articles.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    # "pre_assessment" | "active" | "completed" | "abandoned"
    status: Mapped[str] = mapped_column(String, default="pre_assessment")
    # Serialized SocraticTutor._state — allows stateless reconstruction per turn
    tutor_state_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # analysis results written by analyze_transcript()
    analysis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # "pending" | "running" | "ready" | None
    analysis_status: Mapped[str | None] = mapped_column(String, nullable=True)

    user: Mapped[User] = relationship("User", back_populates="sessions")
    article: Mapped[Article] = relationship("Article", back_populates="sessions")
    turns: Mapped[list[Turn]] = relationship(
        "Turn", back_populates="session", order_by="Turn.turn_number"
    )
    assessments: Mapped[list[Assessment]] = relationship("Assessment", back_populates="session")


# ---------------------------------------------------------------------------
# turns
# ---------------------------------------------------------------------------

class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String, ForeignKey("sessions.id"), nullable=False)
    turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # "user" | "tutor"
    role: Mapped[str] = mapped_column(String, nullable=False)
    # For tutor turns: guardrail-approved response (shown to student)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # For tutor turns: raw pre-guardrail response (used for NAC metric)
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # For tutor turns: SocraticTutor.session_state() snapshot (used for TSU metric)
    tutor_state_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # BKT snapshot written after evaluating user turns (post-hoc analysis)
    evaluator_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    session: Mapped[Session] = relationship("Session", back_populates="turns")


# ---------------------------------------------------------------------------
# bkt_states
# ---------------------------------------------------------------------------

class BKTStateRow(Base):
    __tablename__ = "bkt_states"
    __table_args__ = (
        UniqueConstraint("user_id", "article_id", "kc_id", name="uq_bkt_user_article_kc"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    article_id: Mapped[str] = mapped_column(String, ForeignKey("articles.id"), nullable=False)
    kc_id: Mapped[str] = mapped_column(String, nullable=False)
    p_mastered: Mapped[float] = mapped_column(Float, default=0.10)
    knowledge_class: Mapped[str] = mapped_column(String, default="absent")
    observation_history: Mapped[list] = mapped_column(JSON, default=list)
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship("User", back_populates="bkt_states")
    article: Mapped[Article] = relationship("Article", back_populates="bkt_states")


# ---------------------------------------------------------------------------
# assessments
# ---------------------------------------------------------------------------

class Assessment(Base):
    __tablename__ = "assessments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String, ForeignKey("sessions.id"), nullable=False)
    question_index: Mapped[int] = mapped_column(Integer, nullable=False)
    kc_id: Mapped[str] = mapped_column(String, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    user_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    observation_class: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[Session] = relationship("Session", back_populates="assessments")


# ---------------------------------------------------------------------------
# retention_schedule
# ---------------------------------------------------------------------------

class RetentionSchedule(Base):
    __tablename__ = "retention_schedule"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    article_id: Mapped[str] = mapped_column(String, ForeignKey("articles.id"), nullable=False)
    kc_id: Mapped[str] = mapped_column(String, nullable=False)
    next_review_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    ease_factor: Mapped[float] = mapped_column(Float, default=2.5)  # SM-2

    user: Mapped[User] = relationship("User", back_populates="retention_schedules")
    article: Mapped[Article] = relationship("Article", back_populates="retention_schedules")
