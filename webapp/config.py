"""
webapp/config.py

Central configuration — reads all env vars in one place.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root (one level above this file)
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Required
# ---------------------------------------------------------------------------

# Not used at runtime — all API calls (tutoring, assessment, domain map generation)
# use the X-API-Key header supplied by the client (BYOK). Requests without a key
# are rejected with HTTP 400. Can be left unset.
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{ROOT_DIR / 'webapp.db'}",
)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

SECRET_KEY: str = os.environ.get("SECRET_KEY", "change-me-in-production")
ACCESS_TOKEN_EXPIRE_MINUTES: int = int(
    os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "10080")  # 7 days
)

# ---------------------------------------------------------------------------
# Domain map cache
# ---------------------------------------------------------------------------

DOMAIN_CACHE_DIR: Path = Path(
    os.environ.get("DOMAIN_CACHE_DIR", str(ROOT_DIR / ".socratic-domain-cache"))
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

TUTOR_MODEL: str = os.environ.get("TUTOR_MODEL", "claude-sonnet-4-6")
CLASSIFIER_MODEL: str = os.environ.get("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
DOMAIN_MAPPER_MODEL: str = os.environ.get("DOMAIN_MAPPER_MODEL", "claude-sonnet-4-6")

# ---------------------------------------------------------------------------
# Wikipedia
# ---------------------------------------------------------------------------

WIKIPEDIA_API_BASE: str = "https://en.wikipedia.org/api/rest_v1"

# Maximum characters of article text passed to domain mapper
ARTICLE_MAX_CHARS: int = int(os.environ.get("ARTICLE_MAX_CHARS", "32000"))
