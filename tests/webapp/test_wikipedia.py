"""
tests/webapp/test_wikipedia.py

Unit tests for the Wikipedia service.  Uses httpx mocking — no real HTTP calls.
"""

import pytest
import httpx
import respx

from webapp.services.wikipedia import (
    WikiArticle,
    WikiSection,
    _extract_title,
    _strip_html,
    fetch_article,
)


# ---------------------------------------------------------------------------
# _extract_title
# ---------------------------------------------------------------------------

class TestExtractTitle:
    def test_full_url(self):
        assert _extract_title("https://en.wikipedia.org/wiki/DNA") == "DNA"

    def test_url_with_underscores(self):
        assert _extract_title("https://en.wikipedia.org/wiki/Quantum_entanglement") == "Quantum entanglement"

    def test_url_with_encoded_chars(self):
        title = _extract_title("https://en.wikipedia.org/wiki/G%C3%B6del%27s_incompleteness_theorems")
        assert "Gödel" in title

    def test_plain_title_passthrough(self):
        assert _extract_title("Fractions") == "Fractions"

    def test_strips_whitespace(self):
        assert _extract_title("  DNA  ") == "DNA"


# ---------------------------------------------------------------------------
# _strip_html
# ---------------------------------------------------------------------------

class TestStripHtml:
    def test_removes_tags(self):
        assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_removes_sup_references(self):
        result = _strip_html("Text<sup>[1]</sup> more text")
        assert "[1]" not in result
        assert "Text" in result

    def test_collapses_whitespace(self):
        result = _strip_html("<p>  too   many   spaces  </p>")
        assert "  " not in result.strip()

    def test_empty_string(self):
        assert _strip_html("") == ""


# ---------------------------------------------------------------------------
# WikiArticle.full_text truncation
# ---------------------------------------------------------------------------

class TestWikiArticleFullText:
    def test_truncated_to_max_chars(self, monkeypatch):
        import webapp.config as config
        monkeypatch.setattr(config, "ARTICLE_MAX_CHARS", 50)

        article = WikiArticle(
            page_id=1,
            canonical_title="Test",
            wikipedia_url="https://en.wikipedia.org/wiki/Test",
            summary="",
            sections=[WikiSection(title="Intro", level=1, text="A" * 200)],
        )
        assert len(article.full_text) <= 50

    def test_section_titles_included(self):
        article = WikiArticle(
            page_id=1,
            canonical_title="Test",
            wikipedia_url="https://en.wikipedia.org/wiki/Test",
            summary="",
            sections=[
                WikiSection(title="", level=0, text="Lead text"),
                WikiSection(title="History", level=1, text="Some history"),
            ],
        )
        assert "## History" in article.full_text
        assert "Lead text" in article.full_text


# ---------------------------------------------------------------------------
# fetch_article (mocked HTTP)
# ---------------------------------------------------------------------------

MOCK_SUMMARY = {
    "pageid": 5417747,
    "title": "DNA",
    "extract": "Deoxyribonucleic acid is a polymer.",
    "content_urls": {
        "desktop": {"page": "https://en.wikipedia.org/wiki/DNA"}
    },
}

MOCK_SECTIONS = {
    "lead": {
        "sections": [
            {"content": [{"type": "p", "text": "<p>Intro paragraph.</p>"}]}
        ]
    },
    "remaining": {
        "sections": [
            {
                "line": "<b>Structure</b>",
                "toclevel": 1,
                "content": [{"type": "p", "text": "<p>The double helix.</p>"}],
            }
        ]
    },
}


@pytest.mark.asyncio
@respx.mock
async def test_fetch_article_url():
    respx.get("https://en.wikipedia.org/api/rest_v1/page/summary/DNA").mock(
        return_value=httpx.Response(200, json=MOCK_SUMMARY)
    )
    respx.get("https://en.wikipedia.org/api/rest_v1/page/mobile-sections/DNA").mock(
        return_value=httpx.Response(200, json=MOCK_SECTIONS)
    )

    article = await fetch_article("https://en.wikipedia.org/wiki/DNA")

    assert article.page_id == 5417747
    assert article.canonical_title == "DNA"
    assert "double helix" in article.full_text
    assert len(article.sections) == 2


@pytest.mark.asyncio
@respx.mock
async def test_fetch_article_plain_title():
    respx.get("https://en.wikipedia.org/api/rest_v1/page/summary/Fractions").mock(
        return_value=httpx.Response(200, json={**MOCK_SUMMARY, "title": "Fractions", "pageid": 999})
    )
    respx.get("https://en.wikipedia.org/api/rest_v1/page/mobile-sections/Fractions").mock(
        return_value=httpx.Response(200, json=MOCK_SECTIONS)
    )

    article = await fetch_article("Fractions")
    assert article.canonical_title == "Fractions"
    assert article.page_id == 999


@pytest.mark.asyncio
@respx.mock
async def test_fetch_article_http_error():
    respx.get("https://en.wikipedia.org/api/rest_v1/page/summary/Nonexistent_Page_XYZ").mock(
        return_value=httpx.Response(404)
    )
    with pytest.raises(httpx.HTTPStatusError):
        await fetch_article("Nonexistent Page XYZ")
