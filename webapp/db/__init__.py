"""
webapp/db

Async SQLAlchemy engine + session factory.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webapp import config
from webapp.db.models import Base

engine = create_async_engine(config.DATABASE_URL, echo=False)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

_MIGRATIONS = [
    "ALTER TABLE users    ADD COLUMN credits_remaining INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users    ADD COLUMN is_superuser      BOOLEAN NOT NULL DEFAULT 0",
    "ALTER TABLE articles ADD COLUMN is_published      BOOLEAN NOT NULL DEFAULT 0",
    "ALTER TABLE turns    ADD COLUMN reviewer_verdict  TEXT",
    "ALTER TABLE sessions ADD COLUMN analysis          JSON",
    "ALTER TABLE sessions ADD COLUMN analysis_status   TEXT",
]


async def _run_migrations() -> None:
    """Apply additive schema changes to existing databases.

    Each statement runs in its own transaction so that a failure on one
    (e.g. duplicate column in PostgreSQL) does not abort subsequent ones.
    Fresh databases get the columns via create_all below, so these statements
    are only meaningful for databases created before a migration was added.
    """
    for stmt in _MIGRATIONS:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
        except Exception:
            pass  # column already exists


async def init_db() -> None:
    """Create all tables and run additive migrations."""
    await _run_migrations()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """FastAPI dependency — yields an async session."""
    async with AsyncSessionLocal() as session:
        yield session
