"""Fetch and extract readable content from bookmark destination URLs."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import httpx2 as httpx
import trafilatura

from kairos.bookmarks.urls import is_internal_or_redirect
from kairos.config import settings

logger = logging.getLogger(__name__)

_HTML_TYPE = re.compile(r"text/html|application/xhtml", re.I)
_STRIP_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class LinkPreview:
    """Extracted metadata and body text from a fetched URL."""

    final_url: str
    title: str | None = None
    description: str | None = None
    body_excerpt: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.title or self.description or self.body_excerpt)

    def as_doc_fields(self) -> dict[str, Any]:
        return {
            "link_final_url": self.final_url,
            "link_title": self.title,
            "link_description": self.description,
            "link_body_excerpt": self.body_excerpt,
            "link_fetch_error": self.error,
        }


class _MetaParser(HTMLParser):
    """Fallback OG/title parser when trafilatura returns sparse metadata."""

    def __init__(self) -> None:
        super().__init__()
        self.title: str | None = None
        self.in_title = False
        self.og: dict[str, str] = {}
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            key = (attr.get("property") or attr.get("name") or "").lower()
            content = (attr.get("content") or "").strip()
            if not key or not content:
                return
            if key.startswith("og:"):
                self.og[key[3:]] = content
            else:
                self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            chunk = data.strip()
            if chunk:
                self.title = (self.title or "") + chunk


def _clean_text(text: str, limit: int) -> str:
    collapsed = _WS.sub(" ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _html_to_text(html: str, limit: int) -> str:
    cleaned = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = _STRIP_TAGS.sub(" ", cleaned)
    return _clean_text(text, limit)


def _extract_preview_fallback(html: str, *, max_body: int) -> tuple[str | None, str | None, str | None]:
    parser = _MetaParser()
    try:
        parser.feed(html[:500_000])
    except Exception:  # noqa: BLE001 — best-effort parse
        pass

    title = parser.og.get("title") or parser.title
    description = (
        parser.og.get("description")
        or parser.meta.get("description")
        or parser.meta.get("twitter:description")
    )
    body = _html_to_text(html, max_body)
    if body and len(body) < 40:
        body = None

    return (
        _clean_text(title, 200) if title else None,
        _clean_text(description, 500) if description else None,
        body,
    )


def _extract_preview(html: str, *, max_body: int, url: str = "") -> tuple[str | None, str | None, str | None]:
    """Extract title, description, body — trafilatura first, OG fallback fills gaps."""
    metadata = trafilatura.extract_metadata(html, default_url=url or None)
    title = metadata.title if metadata and metadata.title else None
    description = metadata.description if metadata and metadata.description else None
    body = trafilatura.extract(
        html,
        url=url or None,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    body = _clean_text(body, max_body) if body else None
    if body and len(body) < 40:
        body = None

    fb_title, fb_desc, fb_body = _extract_preview_fallback(html, max_body=max_body)
    title = title or fb_title
    description = description or fb_desc
    body = body or fb_body

    return (
        _clean_text(title, 200) if title else None,
        _clean_text(description, 500) if description else None,
        body,
    )


def pick_fetch_url(doc: dict[str, Any]) -> str | None:
    """URL to fetch — external destinations only (follows t.co redirects)."""
    for candidate in (doc.get("url") or "", doc.get("link_final_url") or ""):
        candidate = candidate.strip()
        if candidate and not is_internal_or_redirect(candidate):
            return candidate
    stored = (doc.get("url") or "").strip()
    if stored and is_internal_or_redirect(stored):
        return stored
    return None


def fetch_link_preview(url: str) -> LinkPreview:
    """GET url, follow redirects, extract title/description/body excerpt."""
    if not settings.link_fetch_enabled:
        return LinkPreview(final_url=url, error="link fetch disabled")

    headers = {
        "User-Agent": settings.link_fetch_user_agent,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    }
    final_url = url
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=settings.link_fetch_timeout_seconds,
            headers=headers,
        ) as client:
            with client.stream("GET", url) as response:
                final_url = str(response.url)
                if is_internal_or_redirect(final_url):
                    return LinkPreview(
                        final_url=final_url,
                        error="resolved to social post — using X API text instead",
                    )
                content_type = response.headers.get("content-type", "")
                if content_type and not _HTML_TYPE.search(content_type):
                    return LinkPreview(
                        final_url=final_url,
                        error=f"unsupported content type: {content_type.split(';')[0]}",
                    )

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > settings.link_fetch_max_bytes:
                        break
                    chunks.append(chunk)
                html = b"".join(chunks).decode("utf-8", errors="replace")
    except httpx.HTTPError as exc:
        logger.warning("Link fetch failed for %s: %s", url, exc)
        return LinkPreview(final_url=url, error=str(exc))

    title, description, body = _extract_preview(
        html,
        max_body=settings.link_fetch_max_body_chars,
        url=final_url,
    )
    return LinkPreview(
        final_url=final_url,
        title=title,
        description=description,
        body_excerpt=body,
    )


def fetch_link_preview_for_bookmark(doc: dict[str, Any]) -> LinkPreview | None:
    """Fetch linked page content for a stored bookmark document."""
    fetch_url = pick_fetch_url(doc)
    if not fetch_url:
        return None
    return fetch_link_preview(fetch_url)


def link_content_text(doc: dict[str, Any]) -> str:
    """Combined fetched page text for research / card display."""
    parts: list[str] = []
    for key in ("link_title", "link_description", "link_body_excerpt"):
        value = _clean_text(doc.get(key) or "", 10_000)
        if value:
            parts.append(value)
    return "\n\n".join(parts)


def needs_link_fetch(doc: dict[str, Any], *, force: bool = False) -> bool:
    if not settings.link_fetch_enabled:
        return False
    if not pick_fetch_url(doc):
        return False
    if force:
        return True
    stored_url = (doc.get("url") or "").strip()
    final = (doc.get("link_final_url") or "").strip()
    if doc.get("link_fetch_error") and not doc.get("link_body_excerpt"):
        return True
    if not final and not doc.get("link_title"):
        return True
    if stored_url and final and stored_url != final and not doc.get("link_body_excerpt"):
        return True
    return False
