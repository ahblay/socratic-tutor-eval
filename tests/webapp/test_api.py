"""
tests/webapp/test_api.py

Integration tests for the FastAPI routes using an in-memory SQLite DB.
No real Wikipedia or Anthropic calls are made — both are mocked.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from unittest.mock import AsyncMock, MagicMock, patch

from webapp.app import create_app
from webapp.db import get_db
from webapp.db.models import Base


# ---------------------------------------------------------------------------
# Test DB — one engine for the whole module, tables recreated per test
# ---------------------------------------------------------------------------

# Use a shared-cache in-memory SQLite so multiple async connections see the same data
TEST_DATABASE_URL = "sqlite+aiosqlite:///file::memory:?cache=shared&uri=true"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def reset_db():
    """Drop and recreate all tables before each test for isolation."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def client():
    """
    HTTP test client with DB and Anthropic overrides.
    We skip the lifespan (no real API key needed) and inject mocks directly.
    """
    app = create_app()
    app.dependency_overrides[get_db] = override_get_db

    # Inject a mock Anthropic client so routes that access app.state don't crash
    import anthropic
    mock_anthropic = AsyncMock(spec=anthropic.AsyncAnthropic)
    mock_anthropic.api_key = "test-key"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        app.state.anthropic_client = mock_anthropic
        yield ac


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(client, email="u@test.com", password="pw") -> str:
    r = await client.post("/api/auth/register", json={"email": email, "password": password})
    return r.json()["access_token"]


async def _anon(client) -> str:
    r = await client.post("/api/auth/anonymous")
    return r.json()["access_token"]


async def _resolve_article(client, page_id=12345, title="DNA", mark_ready=True):
    from webapp.services.wikipedia import WikiArticle, WikiSection
    mock_article = WikiArticle(
        page_id=page_id,
        canonical_title=title,
        wikipedia_url=f"https://en.wikipedia.org/wiki/{title}",
        summary="A polymer.",
        sections=[WikiSection(title="Intro", level=1, text="Some content.")],
    )
    # Patch out the background domain map task — tested separately
    with patch("webapp.api.articles.fetch_article", return_value=mock_article), \
         patch("webapp.api.articles._compute_domain_map_bg", new_callable=AsyncMock):
        r = await client.post("/api/articles/resolve", json={"url": f"https://en.wikipedia.org/wiki/{title}"})
    assert r.status_code == 200
    article_id = r.json()["article_id"]

    if mark_ready:
        async with TestSessionLocal() as db:
            from sqlalchemy import select
            from webapp.db.models import Article
            result = await db.execute(select(Article).where(Article.id == article_id))
            article = result.scalar_one()
            article.domain_map_status = "ready"
            article.domain_map = {
                "topic": title,
                "core_concepts": [
                    {"concept": "A", "description": "", "prerequisite_for": [], "depth_priority": "essential"},
                ],
                "recommended_sequence": ["A"],
                "common_misconceptions": [],
                "checkpoint_questions": [
                    {"after_concept": "A", "question": "What is A?", "what_a_good_answer_demonstrates": "..."}
                ],
            }
            await db.commit()

    return article_id


async def _create_session(client, token, article_id) -> str:
    r = await client.post(
        "/api/sessions",
        json={"article_id": article_id},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    return r.json()["session_id"]


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

class TestAuth:
    async def test_anonymous_session(self, client):
        r = await client.post("/api/auth/anonymous")
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert "user_id" in data

    async def test_register(self, client):
        r = await client.post("/api/auth/register", json={
            "email": "test@example.com", "password": "password123"
        })
        assert r.status_code == 200
        assert "access_token" in r.json()

    async def test_register_duplicate_email(self, client):
        payload = {"email": "dup@example.com", "password": "pw"}
        await client.post("/api/auth/register", json=payload)
        r = await client.post("/api/auth/register", json=payload)
        assert r.status_code == 400

    async def test_login(self, client):
        await client.post("/api/auth/register", json={"email": "login@example.com", "password": "pw123"})
        r = await client.post("/api/auth/login", data={"username": "login@example.com", "password": "pw123"})
        assert r.status_code == 200
        assert "access_token" in r.json()

    async def test_login_wrong_password(self, client):
        await client.post("/api/auth/register", json={"email": "bad@example.com", "password": "correct"})
        r = await client.post("/api/auth/login", data={"username": "bad@example.com", "password": "wrong"})
        assert r.status_code == 400

    async def test_protected_route_requires_token(self, client):
        r = await client.get("/api/sessions/some-id")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Articles routes
# ---------------------------------------------------------------------------

class TestArticles:
    async def test_resolve_article(self, client):
        article_id = await _resolve_article(client, mark_ready=False)
        assert article_id is not None

    async def test_resolve_returns_title(self, client):
        from webapp.services.wikipedia import WikiArticle
        mock = WikiArticle(page_id=1, canonical_title="Fractions",
                           wikipedia_url="https://en.wikipedia.org/wiki/Fractions",
                           summary="A fraction is...", sections=[])
        with patch("webapp.api.articles.fetch_article", return_value=mock), \
             patch("webapp.api.articles._compute_domain_map_bg", new_callable=AsyncMock):
            r = await client.post("/api/articles/resolve", json={"url": "https://en.wikipedia.org/wiki/Fractions"})
        assert r.json()["title"] == "Fractions"

    async def test_resolve_same_page_id_twice_returns_same_id(self, client):
        id1 = await _resolve_article(client, page_id=42, title="Same", mark_ready=False)
        id2 = await _resolve_article(client, page_id=42, title="Same", mark_ready=False)
        assert id1 == id2

    async def test_get_article_not_found(self, client):
        r = await client.get("/api/articles/nonexistent-id")
        assert r.status_code == 404

    async def test_get_article_after_resolve(self, client):
        article_id = await _resolve_article(client, mark_ready=False)
        r = await client.get(f"/api/articles/{article_id}")
        assert r.status_code == 200
        assert r.json()["article_id"] == article_id

    async def test_pending_status_before_domain_map(self, client):
        article_id = await _resolve_article(client, mark_ready=False)
        r = await client.get(f"/api/articles/{article_id}")
        assert r.json()["domain_map_status"] == "pending"

    async def test_kc_count_when_ready(self, client):
        article_id = await _resolve_article(client, mark_ready=True)
        r = await client.get(f"/api/articles/{article_id}")
        assert r.json()["kc_count"] == 1  # one concept in fixture domain map


# ---------------------------------------------------------------------------
# Sessions routes
# ---------------------------------------------------------------------------

class TestSessions:
    async def test_create_session(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _create_session(client, token, article_id)
        assert session_id is not None

    async def test_session_starts_in_pre_assessment(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        r = await client.post(
            "/api/sessions", json={"article_id": article_id},
            headers={"Authorization": f"Bearer {token}"}
        )
        assert r.json()["status"] == "pre_assessment"

    async def test_create_session_domain_map_not_ready(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client, mark_ready=False)
        r = await client.post(
            "/api/sessions", json={"article_id": article_id},
            headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 409

    async def test_create_session_article_not_found(self, client):
        token = await _register(client)
        r = await client.post(
            "/api/sessions", json={"article_id": "bad-id"},
            headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 404

    async def test_get_session(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _create_session(client, token, article_id)
        r = await client.get(
            f"/api/sessions/{session_id}",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200
        assert r.json()["session_id"] == session_id

    async def test_session_not_visible_to_other_user(self, client):
        token = await _register(client, "owner@test.com")
        article_id = await _resolve_article(client)
        session_id = await _create_session(client, token, article_id)

        other_token = await _anon(client)
        r = await client.get(
            f"/api/sessions/{session_id}",
            headers={"Authorization": f"Bearer {other_token}"}
        )
        assert r.status_code == 404

    async def test_turn_rejected_when_pre_assessment(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _create_session(client, token, article_id)
        r = await client.post(
            f"/api/sessions/{session_id}/turn",
            json={"message": "Hello"},
            headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 409

    async def test_end_session(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _create_session(client, token, article_id)
        r = await client.post(
            f"/api/sessions/{session_id}/end",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200
        assert r.json()["status"] == "completed"

    async def test_transcript_empty_new_session(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _create_session(client, token, article_id)
        r = await client.get(
            f"/api/sessions/{session_id}/transcript",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200
        assert r.json()["turns"] == []
