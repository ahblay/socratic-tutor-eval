"""
webapp/api/articles.py

Article resolution and domain map management.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from webapp.api.auth import get_current_user
from webapp.db import get_db
from webapp.db.models import Article, User
from webapp.services.domain_cache import build_kg_from_domain_map, get_or_create_domain_map
from webapp.services.wikipedia import fetch_article

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ResolveRequest(BaseModel):
    url: str


class ArticleResponse(BaseModel):
    article_id: str
    title: str
    wikipedia_url: str
    summary: str
    domain_map_status: str
    kc_count: int


class CatalogArticleResponse(BaseModel):
    article_id: str
    title: str
    summary: str
    kc_count: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", response_model=list[CatalogArticleResponse])
async def list_published_articles(
    db: AsyncSession = Depends(get_db),
):
    """Return all published articles for the lesson catalog. No auth required."""
    result = await db.execute(
        select(Article)
        .where(Article.is_published == True)
        .order_by(Article.canonical_title)
    )
    articles = result.scalars().all()
    return [
        CatalogArticleResponse(
            article_id=a.id,
            title=a.canonical_title,
            summary=a.summary or "",
            kc_count=len(a.domain_map.get("core_concepts", [])) if a.domain_map else 0,
        )
        for a in articles
    ]


@router.post("/resolve", response_model=ArticleResponse)
async def resolve_article(
    body: ResolveRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Accept a Wikipedia URL or title, fetch metadata, and trigger domain map
    generation as a background task if not already cached.
    Restricted to superusers — domain map generation uses the server API key.
    """
    if not user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser access required")

    wiki = await fetch_article(body.url)

    # Upsert article row
    result = await db.execute(
        select(Article).where(Article.wikipedia_page_id == wiki.page_id)
    )
    article = result.scalar_one_or_none()

    if article is None:
        article = Article(
            wikipedia_page_id=wiki.page_id,
            canonical_title=wiki.canonical_title,
            wikipedia_url=wiki.wikipedia_url,
            summary=wiki.summary,
            domain_map_status="pending",
        )
        db.add(article)
        await db.flush()

    # Kick off domain map generation in background if needed
    if article.domain_map_status != "ready":
        article.domain_map_status = "pending"
        lead = wiki.sections[0].text if wiki.sections else wiki.summary or ""
        headings = [s.title for s in wiki.sections if s.title]
        compact_text = lead
        if headings:
            compact_text += "\n\nSection headings:\n" + "\n".join(f"- {h}" for h in headings)
        background_tasks.add_task(
            _compute_domain_map_bg,
            article_id=article.id,
            article_text=compact_text,
        )

    await db.commit()
    await db.refresh(article)

    kc_count = 0
    if article.domain_map:
        kc_count = len(article.domain_map.get("core_concepts", []))

    return ArticleResponse(
        article_id=article.id,
        title=article.canonical_title,
        wikipedia_url=article.wikipedia_url,
        summary=article.summary or "",
        domain_map_status=article.domain_map_status,
        kc_count=kc_count,
    )


@router.get("/featured/today")
async def featured_article(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Resolve today's Wikipedia featured article. Superuser only."""
    import httpx
    from webapp import config

    today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{config.WIKIPEDIA_API_BASE}/feed/featured/{today}",
            headers={"User-Agent": "SocraticTutorBot/1.0 (https://github.com/ahblay/socratic-tutor-eval)"},
        )
        r.raise_for_status()
        data = r.json()

    tfa = data.get("tfa", {})
    url = tfa.get("content_urls", {}).get("desktop", {}).get("page", "")
    if not url:
        raise HTTPException(status_code=503, detail="Featured article not available")

    return await resolve_article(
        ResolveRequest(url=url),
        background_tasks=background_tasks,
        db=db,
        user=user,
    )


@router.get("/{article_id}", response_model=ArticleResponse)
async def get_article(
    article_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Article).where(Article.id == article_id))
    article = result.scalar_one_or_none()
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")

    kc_count = 0
    if article.domain_map:
        kc_count = len(article.domain_map.get("core_concepts", []))

    return ArticleResponse(
        article_id=article.id,
        title=article.canonical_title,
        wikipedia_url=article.wikipedia_url,
        summary=article.summary or "",
        domain_map_status=article.domain_map_status,
        kc_count=kc_count,
    )


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

async def _compute_domain_map_bg(article_id: str, article_text: str) -> None:
    """Run domain map generation in the background using the server API key."""
    import anthropic as _anthropic
    from webapp.db import AsyncSessionLocal

    client = _anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Article).where(Article.id == article_id))
        article = result.scalar_one_or_none()
        if article is None:
            return
        try:
            await get_or_create_domain_map(article, article_text, client, db)
            await db.commit()
        except Exception as e:
            article.domain_map_status = "failed"
            await db.commit()
