"""
webapp/services/wikipedia.py

Wikipedia REST API wrapper.  Fetches article metadata and section-structured
plain text for use as domain mapper input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

from webapp import config

# Wikipedia requires a descriptive User-Agent with contact info (see API:Etiquette)
_USER_AGENT = "SocraticTutorBot/1.0 (https://github.com/ahblay/socratic-tutor-eval)"


@dataclass
class WikiSection:
    title: str
    level: int      # 1 = top-level, 2 = sub-section, …
    text: str       # plain text (HTML stripped)


@dataclass
class WikiArticle:
    page_id: int
    canonical_title: str
    wikipedia_url: str
    summary: str
    sections: list[WikiSection] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        """Section-structured plain text, truncated to ARTICLE_MAX_CHARS."""
        parts = []
        for sec in self.sections:
            if sec.title:
                parts.append(f"\n## {sec.title}\n")
            parts.append(sec.text)
        text = "\n".join(parts).strip()
        return text[: config.ARTICLE_MAX_CHARS]


def _strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    # Remove reference superscripts [1], [2], …
    for tag in soup.find_all("sup"):
        tag.decompose()
    text = soup.get_text(separator=" ")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def fetch_article(url_or_title: str) -> WikiArticle:
    """
    Fetch a Wikipedia article by URL or title.

    Accepts:
    - Full URL: https://en.wikipedia.org/wiki/DNA
    - Title string: "DNA" or "Quantum entanglement"
    """
    title = _extract_title(url_or_title)

    async with httpx.AsyncClient(timeout=15.0) as client:
        summary_data = await _fetch_summary(client, title)
        sections = await _fetch_sections(client, summary_data["canonical_title"])

    return WikiArticle(
        page_id=summary_data["page_id"],
        canonical_title=summary_data["canonical_title"],
        wikipedia_url=summary_data["wikipedia_url"],
        summary=summary_data["summary"],
        sections=sections,
    )


def _extract_title(url_or_title: str) -> str:
    """Extract the article title from a full Wikipedia URL or return as-is."""
    url_or_title = url_or_title.strip()
    # Match https://en.wikipedia.org/wiki/<Title>
    m = re.match(r"https?://en\.wikipedia\.org/wiki/(.+)", url_or_title)
    if m:
        # URL-decode and replace underscores
        from urllib.parse import unquote
        return unquote(m.group(1).replace("_", " "))
    return url_or_title


async def _fetch_summary(client: httpx.AsyncClient, title: str) -> dict:
    url = f"{config.WIKIPEDIA_API_BASE}/page/summary/{_encode_title(title)}"
    resp = client.build_request("GET", url, headers={"User-Agent": _USER_AGENT})
    r = await client.send(resp)
    r.raise_for_status()
    data = r.json()
    return {
        "page_id": data["pageid"],
        "canonical_title": data["title"],
        "wikipedia_url": data["content_urls"]["desktop"]["page"],
        "summary": data.get("extract", ""),
    }


async def _fetch_sections(client: httpx.AsyncClient, title: str) -> list[WikiSection]:
    """Fetch plain-text article content via the MediaWiki Action API (extracts module)."""
    params = {
        "action": "query",
        "prop": "extracts",
        "titles": title,
        "explaintext": "1",
        "exsectionformat": "wiki",  # section headers as == Title ==
        "redirects": "1",
        "format": "json",
    }
    resp = client.build_request(
        "GET", "https://en.wikipedia.org/w/api.php",
        params=params,
        headers={"User-Agent": _USER_AGENT},
    )
    r = await client.send(resp)
    r.raise_for_status()

    pages = r.json().get("query", {}).get("pages", {})
    extract = next(iter(pages.values()), {}).get("extract", "")
    if not extract:
        return []

    # Parse == Title == / === Title === markers into WikiSection objects
    sections: list[WikiSection] = []
    current_title = ""
    current_level = 0
    current_lines: list[str] = []

    def _flush():
        text = " ".join(current_lines).strip()
        if text:
            sections.append(WikiSection(title=current_title, level=current_level, text=text))

    for line in extract.split("\n"):
        m = re.match(r"^(={2,5})\s*(.+?)\s*\1\s*$", line)
        if m:
            _flush()
            current_title = m.group(2)
            current_level = len(m.group(1)) - 1  # == → 1, === → 2, …
            current_lines = []
        else:
            stripped = line.strip()
            if stripped:
                current_lines.append(stripped)

    _flush()
    return sections


def _encode_title(title: str) -> str:
    from urllib.parse import quote
    return quote(title.replace(" ", "_"), safe="")
