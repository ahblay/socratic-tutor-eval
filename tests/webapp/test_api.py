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
    r = await client.post("/api/auth/register", json={"email": email, "password": password, "consented": True})
    return r.json()["access_token"]


async def _register_superuser(client, email="admin@test.com", password="pw") -> str:
    """Register a user (or log in if already registered) and promote to superuser.

    Idempotent: safe to call multiple times within the same test.
    """
    r = await client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "consented": True},
    )
    if r.status_code == 400:  # email already registered
        r = await client.post("/api/auth/login", data={"username": email, "password": password})
    data = r.json()
    token = data["access_token"]
    user_id = data["user_id"]
    async with TestSessionLocal() as db:
        from sqlalchemy import select
        from webapp.db.models import User
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one()
        user.is_superuser = True
        await db.commit()
    return token


async def _resolve_article(client, page_id=12345, title="DNA", mark_ready=True):
    from webapp.services.wikipedia import WikiArticle, WikiSection
    mock_article = WikiArticle(
        page_id=page_id,
        canonical_title=title,
        wikipedia_url=f"https://en.wikipedia.org/wiki/{title}",
        summary="A polymer.",
        sections=[WikiSection(title="Intro", level=1, text="Some content.")],
    )
    admin_token = await _register_superuser(client)
    # Patch out the background domain map task — tested separately
    with patch("webapp.api.articles.fetch_article", return_value=mock_article), \
         patch("webapp.api.articles._compute_domain_map_bg", new_callable=AsyncMock):
        r = await client.post(
            "/api/articles/resolve",
            json={"url": f"https://en.wikipedia.org/wiki/{title}"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
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
    # Decode token to get user_id and grant credits so session creation succeeds.
    # Tests that specifically test the 0-credit path do not use this helper.
    from jose import jwt as jose_jwt
    from webapp import config
    payload = jose_jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])
    user_id = payload["sub"]
    async with TestSessionLocal() as db:
        from sqlalchemy import select
        from webapp.db.models import User
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one()
        user.credits_remaining = 50
        await db.commit()

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
    async def test_register(self, client):
        r = await client.post("/api/auth/register", json={
            "email": "test@example.com", "password": "password123", "consented": True
        })
        assert r.status_code == 200
        assert "access_token" in r.json()

    async def test_register_duplicate_email(self, client):
        payload = {"email": "dup@example.com", "password": "pw", "consented": True}
        await client.post("/api/auth/register", json=payload)
        r = await client.post("/api/auth/register", json=payload)
        assert r.status_code == 400

    async def test_login(self, client):
        await client.post("/api/auth/register", json={"email": "login@example.com", "password": "pw123", "consented": True})
        r = await client.post("/api/auth/login", data={"username": "login@example.com", "password": "pw123"})
        assert r.status_code == 200
        assert "access_token" in r.json()

    async def test_login_wrong_password(self, client):
        await client.post("/api/auth/register", json={"email": "bad@example.com", "password": "correct", "consented": True})
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
        admin_token = await _register_superuser(client)
        mock = WikiArticle(page_id=1, canonical_title="Fractions",
                           wikipedia_url="https://en.wikipedia.org/wiki/Fractions",
                           summary="A fraction is...", sections=[])
        with patch("webapp.api.articles.fetch_article", return_value=mock), \
             patch("webapp.api.articles._compute_domain_map_bg", new_callable=AsyncMock):
            r = await client.post(
                "/api/articles/resolve",
                json={"url": "https://en.wikipedia.org/wiki/Fractions"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        assert r.json()["title"] == "Fractions"

    async def test_resolve_requires_superuser(self, client):
        token = await _register(client)
        with patch("webapp.api.articles.fetch_article"), \
             patch("webapp.api.articles._compute_domain_map_bg", new_callable=AsyncMock):
            r = await client.post(
                "/api/articles/resolve",
                json={"url": "https://en.wikipedia.org/wiki/DNA"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 403

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

    async def test_catalog_empty_when_nothing_published(self, client):
        await _resolve_article(client, mark_ready=True)  # resolved but not published
        r = await client.get("/api/articles")
        assert r.status_code == 200
        assert r.json() == []

    async def test_catalog_returns_published_articles(self, client):
        admin_token = await _register_superuser(client)
        article_id = await _resolve_article(client, mark_ready=True)
        await client.post(
            f"/api/admin/articles/{article_id}/publish",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        r = await client.get("/api/articles")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["article_id"] == article_id

    async def test_catalog_excludes_unpublished(self, client):
        admin_token = await _register_superuser(client)
        article_id = await _resolve_article(client, mark_ready=True)
        await client.post(
            f"/api/admin/articles/{article_id}/publish",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        await client.post(
            f"/api/admin/articles/{article_id}/unpublish",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        r = await client.get("/api/articles")
        assert r.json() == []

    async def test_catalog_no_auth_required(self, client):
        r = await client.get("/api/articles")
        assert r.status_code == 200  # no Authorization header needed


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
        # Grant credits so session creation is not blocked
        from jose import jwt as jose_jwt
        from webapp import config
        user_id = jose_jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])["sub"]
        async with TestSessionLocal() as db:
            from sqlalchemy import select
            from webapp.db.models import User
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one()
            user.credits_remaining = 5
            await db.commit()
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

        other_token = await _register(client, "other@test.com")
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


# ---------------------------------------------------------------------------
# Turn hot path
# ---------------------------------------------------------------------------

async def _activate_session(client, token, article_id) -> str:
    """Create a session and manually set it to active (bypassing assessment)."""
    session_id = await _create_session(client, token, article_id)
    async with TestSessionLocal() as db:
        from sqlalchemy import select
        from webapp.db.models import Session
        result = await db.execute(select(Session).where(Session.id == session_id))
        session = result.scalar_one()
        session.status = "active"
        await db.commit()
    return session_id


class TestTurnHotPath:
    async def test_turn_requires_active_session(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _create_session(client, token, article_id)
        # Session is pre_assessment, not active
        r = await client.post(
            f"/api/sessions/{session_id}/turn",
            json={"message": "Hello"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 409

    async def test_turn_returns_reply(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _activate_session(client, token, article_id)

        with patch("tutor_eval.tutors.socratic.SocraticTutor.respond", return_value="What do you think?") as mock_respond:
            r = await client.post(
                f"/api/sessions/{session_id}/turn",
                json={"message": "I think DNA is made of cells."},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200
        assert r.json()["reply"] == "What do you think?"
        assert r.json()["turn_number"] == 1

    async def test_turn_saves_transcript(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _activate_session(client, token, article_id)

        with patch("tutor_eval.tutors.socratic.SocraticTutor.respond", return_value="Can you elaborate?"):
            await client.post(
                f"/api/sessions/{session_id}/turn",
                json={"message": "DNA is a molecule."},
                headers={"Authorization": f"Bearer {token}"},
            )

        r = await client.get(
            f"/api/sessions/{session_id}/transcript",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        turns = r.json()["turns"]
        assert len(turns) == 2  # student turn + tutor turn
        roles = [t["role"] for t in turns]
        assert roles == ["user", "tutor"]

    async def test_turn_increments_turn_count(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _activate_session(client, token, article_id)

        with patch("tutor_eval.tutors.socratic.SocraticTutor.respond", return_value="Interesting."):
            await client.post(
                f"/api/sessions/{session_id}/turn",
                json={"message": "First message."},
                headers={"Authorization": f"Bearer {token}"},
            )
            await client.post(
                f"/api/sessions/{session_id}/turn",
                json={"message": "Second message."},
                headers={"Authorization": f"Bearer {token}"},
            )

        r = await client.get(
            f"/api/sessions/{session_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.json()["turn_count"] == 2

    async def test_turn_rejects_completed_session(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _activate_session(client, token, article_id)

        await client.post(
            f"/api/sessions/{session_id}/end",
            headers={"Authorization": f"Bearer {token}"},
        )

        with patch("tutor_eval.tutors.socratic.SocraticTutor.respond", return_value="Too late."):
            r = await client.post(
                f"/api/sessions/{session_id}/turn",
                json={"message": "Hello?"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# Assessment fixtures and helpers
# ---------------------------------------------------------------------------

RICH_DOMAIN_MAP = {
    "topic": "DNA",
    "core_concepts": [
        {
            "concept": "Nucleotides",
            "description": "Building blocks of DNA",
            "prerequisite_for": ["Double Helix"],
            "depth_priority": "essential",
        },
        {
            "concept": "Double Helix",
            "description": "The twisted-ladder structure of DNA",
            "prerequisite_for": [],
            "depth_priority": "essential",
        },
    ],
    "recommended_sequence": ["Nucleotides", "Double Helix"],
    "common_misconceptions": [],
    "checkpoint_questions": [
        {
            "after_concept": "Nucleotides",
            "question": "What is a nucleotide?",
            "what_a_good_answer_demonstrates": "...",
        },
        {
            "after_concept": "Double Helix",
            "question": "Describe the double helix structure.",
            "what_a_good_answer_demonstrates": "...",
        },
    ],
}


async def _resolve_rich_article(client) -> str:
    """Resolve an article with a two-concept domain map (for propagation tests)."""
    from webapp.services.wikipedia import WikiArticle, WikiSection
    mock_article = WikiArticle(
        page_id=99999,
        canonical_title="DNA",
        wikipedia_url="https://en.wikipedia.org/wiki/DNA",
        summary="DNA is a molecule.",
        sections=[WikiSection(title="Intro", level=1, text="Some content.")],
    )
    admin_token = await _register_superuser(client)
    with patch("webapp.api.articles.fetch_article", return_value=mock_article), \
         patch("webapp.api.articles._compute_domain_map_bg", new_callable=AsyncMock):
        r = await client.post(
            "/api/articles/resolve",
            json={"url": "https://en.wikipedia.org/wiki/DNA"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    article_id = r.json()["article_id"]

    async with TestSessionLocal() as db:
        from sqlalchemy import select
        from webapp.db.models import Article
        result = await db.execute(select(Article).where(Article.id == article_id))
        article = result.scalar_one()
        article.domain_map_status = "ready"
        article.domain_map = RICH_DOMAIN_MAP
        await db.commit()

    return article_id


async def _start_assessment(client, token, session_id) -> str:
    r = await client.post(
        f"/api/sessions/{session_id}/assessment/start",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    return r.json()["question_text"]


async def _answer(client, token, session_id, answer: str, mock_class: str = "partial"):
    with patch("webapp.api.assessment.classify_opener_answer", return_value=mock_class), \
         patch("webapp.api.assessment.generate_followup_question", return_value="Tell me more."), \
         patch("webapp.api.assessment.classify_full_assessment", return_value={}):
        r = await client.post(
            f"/api/sessions/{session_id}/assessment/answer",
            json={"answer": answer},
            headers={"Authorization": f"Bearer {token}"},
        )
    return r


# ---------------------------------------------------------------------------
# Assessment integration tests
# ---------------------------------------------------------------------------

class TestAssessment:
    async def test_start_creates_opener_question(self, client):
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)

        r = await client.post(
            f"/api/sessions/{session_id}/assessment/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["question_index"] == 0
        assert data["kc_id"] == "__opener__"
        assert "DNA" in data["question_text"]
        assert data["assessment_complete"] is False

    async def test_start_idempotent(self, client):
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)

        r1 = await client.post(
            f"/api/sessions/{session_id}/assessment/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        r2 = await client.post(
            f"/api/sessions/{session_id}/assessment/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["question_text"] == r2.json()["question_text"]

        # Only one Assessment row should exist
        async with TestSessionLocal() as db:
            from sqlalchemy import select, func
            from webapp.db.models import Assessment
            result = await db.execute(
                select(func.count()).where(Assessment.session_id == session_id)
            )
            assert result.scalar() == 1

    async def test_start_requires_pre_assessment_status(self, client):
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _activate_session(client, token, article_id)

        r = await client.post(
            f"/api/sessions/{session_id}/assessment/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 409

    async def test_answer_without_start_returns_409(self, client):
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)

        r = await _answer(client, token, session_id, "I don't know.")
        assert r.status_code == 409

    async def test_answer_opener_returns_followup(self, client):
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)
        await _start_assessment(client, token, session_id)

        r = await _answer(client, token, session_id, "I know a little.", mock_class="partial")
        assert r.status_code == 200
        data = r.json()
        assert data["assessment_complete"] is False
        assert data["question_index"] == 1
        assert data["question_text"] is not None
        assert data["observation_class"] == "partial"

    async def test_answer_opener_issues_followup(self, client):
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)
        await _start_assessment(client, token, session_id)

        r = await _answer(client, token, session_id, "I know DNA well.", mock_class="mastered")
        assert r.status_code == 200
        # Opener answer should issue a follow-up (not complete assessment immediately)
        assert r.json()["assessment_complete"] is False
        assert r.json()["question_text"] == "Tell me more."

    async def test_answer_saves_observation_class(self, client):
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)
        await _start_assessment(client, token, session_id)

        await _answer(client, token, session_id, "Some answer.", mock_class="partial")

        async with TestSessionLocal() as db:
            from sqlalchemy import select
            from webapp.db.models import Assessment
            result = await db.execute(
                select(Assessment).where(
                    Assessment.session_id == session_id,
                    Assessment.question_index == 0,
                )
            )
            row = result.scalar_one()
            assert row.observation_class == "partial"
            assert row.user_answer == "Some answer."

    async def test_answer_wrong_status_returns_409(self, client):
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _activate_session(client, token, article_id)

        r = await _answer(client, token, session_id, "Hello.")
        assert r.status_code == 409

    async def test_complete_transitions_session_to_active(self, client):
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)
        await _start_assessment(client, token, session_id)
        await _answer(client, token, session_id, "I know a bit.", mock_class="partial")

        r = await client.post(
            f"/api/sessions/{session_id}/assessment/complete",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "active"

        r2 = await client.get(
            f"/api/sessions/{session_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.json()["status"] == "active"

    async def test_complete_writes_bkt_rows_for_all_kcs(self, client):
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)
        await _start_assessment(client, token, session_id)
        await _answer(client, token, session_id, "I know a bit.", mock_class="partial")

        r = await client.post(
            f"/api/sessions/{session_id}/assessment/complete",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.json()["bkt_initialized"] == 2  # Nucleotides + Double Helix

    async def test_complete_without_answers_returns_409(self, client):
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)

        r = await client.post(
            f"/api/sessions/{session_id}/assessment/complete",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 409

    async def test_complete_already_active_returns_409(self, client):
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)
        await _start_assessment(client, token, session_id)
        await _answer(client, token, session_id, "I know.", mock_class="partial")

        await client.post(
            f"/api/sessions/{session_id}/assessment/complete",
            headers={"Authorization": f"Bearer {token}"},
        )
        r = await client.post(
            f"/api/sessions/{session_id}/assessment/complete",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 409

    async def test_bkt_propagation_mastered_raises_prerequisite(self, client):
        """If student masters Double Helix, Nucleotides (its prereq) should get raised L0."""
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)
        await _start_assessment(client, token, session_id)

        # Opener: partial
        await _answer(client, token, session_id, "Some knowledge.", mock_class="partial")
        # Follow-up: describe Double Helix → holistic classifier marks it mastered
        await _answer(client, token, session_id, "DNA has a double helix structure.", mock_class="partial")

        # complete_assessment runs holistic classification: double-helix mastered → nucleotides raised
        with patch("webapp.api.assessment.classify_full_assessment",
                   return_value={"double-helix": "mastered", "nucleotides": "partial"}):
            await client.post(
                f"/api/sessions/{session_id}/assessment/complete",
                headers={"Authorization": f"Bearer {token}"},
            )

        async with TestSessionLocal() as db:
            from sqlalchemy import select
            from webapp.db.models import BKTStateRow
            result = await db.execute(
                select(BKTStateRow).where(
                    BKTStateRow.session_id == session_id if hasattr(BKTStateRow, "session_id") else BKTStateRow.article_id == article_id,
                    BKTStateRow.kc_id == "nucleotides",
                )
            )
            row = result.scalar_one_or_none()
            assert row is not None
            assert row.p_mastered >= 0.85

    async def test_bkt_propagation_absent_lowers_dependent(self, client):
        """If student has absent knowledge of Nucleotides (prereq), Double Helix should be lowered."""
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)
        await _start_assessment(client, token, session_id)

        # Opener: absent
        await _answer(client, token, session_id, "I don't know.", mock_class="absent")
        # Follow-up: no knowledge of nucleotides
        await _answer(client, token, session_id, "No idea.", mock_class="absent")

        # complete_assessment runs holistic classification: nucleotides absent → double-helix lowered
        with patch("webapp.api.assessment.classify_full_assessment",
                   return_value={"nucleotides": "absent", "double-helix": "absent"}):
            await client.post(
                f"/api/sessions/{session_id}/assessment/complete",
                headers={"Authorization": f"Bearer {token}"},
            )

        async with TestSessionLocal() as db:
            from sqlalchemy import select
            from webapp.db.models import BKTStateRow
            result = await db.execute(
                select(BKTStateRow).where(
                    BKTStateRow.article_id == article_id,
                    BKTStateRow.kc_id == "double-helix",
                )
            )
            row = result.scalar_one_or_none()
            assert row is not None
            assert row.p_mastered <= 0.15

    async def test_turn_works_after_assessment_complete(self, client):
        """Full flow: assessment → active → turn accepted."""
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)
        await _start_assessment(client, token, session_id)
        await _answer(client, token, session_id, "Some knowledge.", mock_class="partial")
        await client.post(
            f"/api/sessions/{session_id}/assessment/complete",
            headers={"Authorization": f"Bearer {token}"},
        )

        with patch("tutor_eval.tutors.socratic.SocraticTutor.respond", return_value="What do you think?"):
            r = await client.post(
                f"/api/sessions/{session_id}/turn",
                json={"message": "Hello."},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200

    async def test_turn_rejected_during_assessment(self, client):
        """Turn must be rejected while session is still in pre_assessment (regression guard)."""
        token = await _register(client)
        article_id = await _resolve_rich_article(client)
        session_id = await _create_session(client, token, article_id)

        with patch("tutor_eval.tutors.socratic.SocraticTutor.respond", return_value="Hi."):
            r = await client.post(
                f"/api/sessions/{session_id}/turn",
                json={"message": "Hello."},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# Assessment service unit tests (no HTTP, no DB)
# ---------------------------------------------------------------------------

class TestAssessmentService:
    def test_max_followups_is_five(self):
        from webapp.services.assessment_service import MAX_FOLLOWUPS
        assert MAX_FOLLOWUPS == 5

    def test_propagate_l0_global_prior_fills_unassessed(self):
        from webapp.services.assessment_service import propagate_l0
        dm = {
            "core_concepts": [
                {"concept": "X", "description": "", "prerequisite_for": [], "depth_priority": "essential"},
                {"concept": "Y", "description": "", "prerequisite_for": [], "depth_priority": "essential"},
            ],
            "recommended_sequence": ["X", "Y"],
        }
        result = propagate_l0(dm, assessed_kcs={}, global_prior="partial")
        assert abs(result["x"] - 0.25) < 0.01
        assert abs(result["y"] - 0.25) < 0.01

    def test_propagate_l0_mastered_raises_prereq(self):
        from webapp.services.assessment_service import propagate_l0
        dm = {
            "core_concepts": [
                {"concept": "Prereq", "description": "", "prerequisite_for": ["Dep"], "depth_priority": "essential"},
                {"concept": "Dep", "description": "", "prerequisite_for": [], "depth_priority": "essential"},
            ],
            "recommended_sequence": ["Prereq", "Dep"],
        }
        # Student masters Dep → Prereq should be raised
        result = propagate_l0(dm, assessed_kcs={"dep": "mastered"}, global_prior="absent")
        assert result["prereq"] >= 0.85

    def test_propagate_l0_absent_lowers_dependent(self):
        from webapp.services.assessment_service import propagate_l0
        dm = {
            "core_concepts": [
                {"concept": "Prereq", "description": "", "prerequisite_for": ["Dep"], "depth_priority": "essential"},
                {"concept": "Dep", "description": "", "prerequisite_for": [], "depth_priority": "essential"},
            ],
            "recommended_sequence": ["Prereq", "Dep"],
        }
        # Student absent on Prereq → Dep should be lowered
        result = propagate_l0(dm, assessed_kcs={"prereq": "absent"}, global_prior="mastered")
        assert result["dep"] <= 0.15

    def test_propagate_l0_clamps_to_bounds(self):
        from webapp.services.assessment_service import propagate_l0
        dm = {
            "core_concepts": [
                {"concept": "Z", "description": "", "prerequisite_for": [], "depth_priority": "essential"},
            ],
            "recommended_sequence": ["Z"],
        }
        result = propagate_l0(dm, assessed_kcs={"z": "mastered"}, global_prior="mastered")
        assert all(0.01 <= v <= 0.99 for v in result.values())

    def test_class_from_l0_thresholds(self):
        from webapp.services.assessment_service import class_from_l0
        assert class_from_l0(0.90) == "mastered"
        assert class_from_l0(0.70) == "mastered"
        assert class_from_l0(0.69) == "partial"
        assert class_from_l0(0.30) == "partial"
        assert class_from_l0(0.29) == "absent"
        assert class_from_l0(0.10) == "absent"


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

class TestAdmin:
    async def test_non_superuser_cannot_add_credits(self, client):
        token = await _register(client)
        r = await client.post(
            "/api/admin/users/some-id/credits",
            json={"amount": 10},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    async def test_non_superuser_cannot_publish(self, client):
        token = await _register(client)
        r = await client.post(
            "/api/admin/articles/some-id/publish",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    async def test_non_superuser_cannot_list_users(self, client):
        token = await _register(client)
        r = await client.get(
            "/api/admin/users",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    async def test_add_credits_increases_balance(self, client):
        admin_token = await _register_superuser(client)
        user_token = await _register(client, "student@test.com")
        user_id = (await client.post(
            "/api/auth/login", data={"username": "student@test.com", "password": "pw"}
        )).json()["user_id"]

        r = await client.post(
            f"/api/admin/users/{user_id}/credits",
            json={"amount": 25},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        assert r.json()["credits_remaining"] == 25

        # Adding more credits accumulates
        r2 = await client.post(
            f"/api/admin/users/{user_id}/credits",
            json={"amount": 10},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r2.json()["credits_remaining"] == 35

    async def test_add_credits_zero_rejected(self, client):
        admin_token = await _register_superuser(client)
        r = await client.post(
            "/api/admin/users/any-id/credits",
            json={"amount": 0},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 422  # Pydantic Field(gt=0) validation

    async def test_add_credits_user_not_found(self, client):
        admin_token = await _register_superuser(client)
        r = await client.post(
            "/api/admin/users/nonexistent/credits",
            json={"amount": 5},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 404

    async def test_publish_article(self, client):
        admin_token = await _register_superuser(client)
        article_id = await _resolve_article(client, mark_ready=True)

        r = await client.post(
            f"/api/admin/articles/{article_id}/publish",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        assert r.json()["is_published"] is True

    async def test_publish_requires_ready_domain_map(self, client):
        admin_token = await _register_superuser(client)
        article_id = await _resolve_article(client, mark_ready=False)

        r = await client.post(
            f"/api/admin/articles/{article_id}/publish",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 409

    async def test_unpublish_article(self, client):
        admin_token = await _register_superuser(client)
        article_id = await _resolve_article(client, mark_ready=True)

        await client.post(
            f"/api/admin/articles/{article_id}/publish",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        r = await client.post(
            f"/api/admin/articles/{article_id}/unpublish",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        assert r.json()["is_published"] is False

    async def test_list_users(self, client):
        admin_token = await _register_superuser(client)
        await _register(client, "a@test.com")
        await _register(client, "b@test.com")

        r = await client.get(
            "/api/admin/users",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200
        emails = [u["email"] for u in r.json()]
        assert "a@test.com" in emails
        assert "b@test.com" in emails


# ---------------------------------------------------------------------------
# Credit enforcement
# ---------------------------------------------------------------------------

class TestCredits:
    async def test_create_session_blocked_with_no_credits(self, client):
        token = await _register(client)  # 0 credits
        article_id = await _resolve_article(client)
        r = await client.post(
            "/api/sessions",
            json={"article_id": article_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 402

    async def test_create_session_allowed_with_credits(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        # Grant credits via DB
        from jose import jwt as jose_jwt
        from webapp import config
        user_id = jose_jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])["sub"]
        async with TestSessionLocal() as db:
            from sqlalchemy import select
            from webapp.db.models import User
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one()
            user.credits_remaining = 5
            await db.commit()
        r = await client.post(
            "/api/sessions",
            json={"article_id": article_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200

    async def test_resume_bypasses_credit_check(self, client):
        """Returning to an existing active session works even with 0 credits."""
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _create_session(client, token, article_id)  # grants 50 credits

        # Drain all credits
        from jose import jwt as jose_jwt
        from webapp import config
        user_id = jose_jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])["sub"]
        async with TestSessionLocal() as db:
            from sqlalchemy import select
            from webapp.db.models import User
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one()
            user.credits_remaining = 0
            await db.commit()

        # POST /sessions should return the existing session, not 402
        r = await client.post(
            "/api/sessions",
            json={"article_id": article_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["session_id"] == session_id

    async def test_turn_blocked_with_no_credits(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _activate_session(client, token, article_id)  # grants credits internally

        # Drain credits
        from jose import jwt as jose_jwt
        from webapp import config
        user_id = jose_jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])["sub"]
        async with TestSessionLocal() as db:
            from sqlalchemy import select
            from webapp.db.models import User
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one()
            user.credits_remaining = 0
            await db.commit()

        with patch("tutor_eval.tutors.socratic.SocraticTutor.respond", return_value="Hi."):
            r = await client.post(
                f"/api/sessions/{session_id}/turn",
                json={"message": "Hello"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 402

    async def test_turn_decrements_credits(self, client):
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _activate_session(client, token, article_id)

        from jose import jwt as jose_jwt
        from webapp import config
        user_id = jose_jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])["sub"]

        with patch("tutor_eval.tutors.socratic.SocraticTutor.respond", return_value="Think about it."):
            await client.post(
                f"/api/sessions/{session_id}/turn",
                json={"message": "Hello."},
                headers={"Authorization": f"Bearer {token}"},
            )

        async with TestSessionLocal() as db:
            from sqlalchemy import select
            from webapp.db.models import User
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one()
            assert user.credits_remaining == 49  # started at 50, used 1

    async def test_superuser_turn_does_not_decrement_credits(self, client):
        admin_token = await _register_superuser(client)
        article_id = await _resolve_article(client)
        session_id = await _activate_session(client, admin_token, article_id)

        from jose import jwt as jose_jwt
        from webapp import config
        user_id = jose_jwt.decode(admin_token, config.SECRET_KEY, algorithms=["HS256"])["sub"]

        with patch("tutor_eval.tutors.socratic.SocraticTutor.respond", return_value="Good question."):
            r = await client.post(
                f"/api/sessions/{session_id}/turn",
                json={"message": "Hello."},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        assert r.status_code == 200

        async with TestSessionLocal() as db:
            from sqlalchemy import select
            from webapp.db.models import User
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one()
            assert user.credits_remaining == 50  # superuser credits unchanged (not decremented)

    async def test_turn_402_leaves_session_active(self, client):
        """Credits exhausted mid-session: session stays active for resumption."""
        token = await _register(client)
        article_id = await _resolve_article(client)
        session_id = await _activate_session(client, token, article_id)

        from jose import jwt as jose_jwt
        from webapp import config
        user_id = jose_jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])["sub"]
        async with TestSessionLocal() as db:
            from sqlalchemy import select
            from webapp.db.models import User
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one()
            user.credits_remaining = 0
            await db.commit()

        with patch("tutor_eval.tutors.socratic.SocraticTutor.respond", return_value="Hi."):
            await client.post(
                f"/api/sessions/{session_id}/turn",
                json={"message": "Hello"},
                headers={"Authorization": f"Bearer {token}"},
            )

        r = await client.get(
            f"/api/sessions/{session_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.json()["status"] == "active"
