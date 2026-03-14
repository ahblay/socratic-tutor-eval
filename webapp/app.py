"""
webapp/app.py

FastAPI application factory.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pathlib

import anthropic
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from webapp import config
from webapp.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    app.state.anthropic_client = anthropic.AsyncAnthropic(
        api_key=config.ANTHROPIC_API_KEY or None  # None → reads from env
    )
    yield
    # Shutdown (nothing to clean up yet)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Socratic Tutor",
        description="Wikipedia-based Socratic tutoring platform",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS — permits the browser extension to deep-link
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )

    # Routers (registered here as they are implemented)
    from webapp.api import articles, sessions, assessment, auth, export
    app.include_router(articles.router, prefix="/api/articles", tags=["articles"])
    app.include_router(sessions.router, prefix="/api/sessions", tags=["sessions"])
    app.include_router(assessment.router, prefix="/api/sessions", tags=["assessment"])
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(export.router, prefix="/api/export", tags=["export"])

    # Static files and templates
    static_dir = pathlib.Path(__file__).parent / "static"
    templates_dir = pathlib.Path(__file__).parent / "templates"
    static_dir.mkdir(exist_ok=True)
    templates_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    templates = Jinja2Templates(directory=str(templates_dir))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("index.html", {"request": request})

    return app


app = create_app()
