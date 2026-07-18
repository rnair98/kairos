"""Upfront web research on a bookmark via Exa retrieval + Gemini synthesis.

Exa search retrieves current web context, then one Gemini call synthesizes a
summary / validation signal / status. Sources come from Exa citations.
Runs at enrich time (kairos bookmarks research), not in the live heartbeat path.
"""

from __future__ import annotations

import re

from kairos.bookmarks.urls import is_bare_url
from kairos.config import settings
from kairos.llm.grounding import search_web
from kairos.llm.interactions import create_interaction
from kairos.models.schemas import BookmarkResearch, RelevanceStatus, UrlCitation

_VALID_STATUS: set[str] = {"current", "dated", "stale", "unknown"}

_SYSTEM = (
    "You pre-research a saved bookmark so its owner can judge relevance in one glance. "
    "The input may include fetched page content (title, description, article text) from the "
    "linked URL, tweet context, and retrieved web context. Be concise and factual. "
    "Return exactly three lines, no preamble:\n"
    "SUMMARY: <1-2 sentences: what this is and why it mattered>\n"
    "SIGNAL: <one short clause on whether it's still worth opening — e.g. "
    "'still the canonical reference', 'superseded by X', 'author shipped v2 since', "
    "'thread deleted, archived only'>\n"
    "STATUS: <current|dated|stale>"
)


def _parse_line(text: str, key: str) -> str | None:
    match = re.search(rf"^{key}:\s*(.+)$", text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _coerce_status(raw: str | None) -> RelevanceStatus:
    if raw:
        token = raw.strip().lower().split()[0].strip(".,:")
        if token in _VALID_STATUS:
            return token  # type: ignore[return-value]
    return "unknown"


def research_bookmark(
    raw_text: str,
    url: str,
    *,
    skip_grounding: bool = False,
) -> BookmarkResearch:
    """Research a bookmark: optional Exa retrieval, then one Gemini synthesis call."""
    max_chars = settings.enrich_max_input_chars
    skip = skip_grounding or settings.grounding_provider != "exa"
    model = settings.gemini_flash_lite_model if skip else settings.gemini_model

    retrieved_block = ""
    citations: list[UrlCitation] = []
    if not skip:
        retrieved = search_web((raw_text or url)[:200])
        if retrieved.text:
            retrieved_block = f"\n\nRetrieved web context:\n{retrieved.text}"
            citations = retrieved.citations

    interaction = create_interaction(
        label="bookmark-research-fast" if skip else "bookmark-research",
        model=model,
        input=(
            "Research this saved bookmark. Prefer the fetched page content when present.\n\n"
            f"URL: {url}\n\n"
            f"Context:\n{raw_text[:max_chars]}"
            f"{retrieved_block}"
        ),
        system_instruction=_SYSTEM,
        store=False,
    )
    text = (interaction.output_text or "").strip()

    summary = _parse_line(text, "SUMMARY")
    signal = _parse_line(text, "SIGNAL")
    status = _coerce_status(_parse_line(text, "STATUS"))

    if not summary or is_bare_url(summary):
        if text and not is_bare_url(text):
            summary = text[:300]
        elif raw_text and not is_bare_url(raw_text):
            summary = raw_text[:200].strip()
        else:
            summary = "Link bookmark — web preview unavailable; open to inspect."
    if not signal:
        signal = "No additional web context found."

    return BookmarkResearch(
        research_summary=summary,
        relevance_signal=signal,
        relevance_status=status,
        research_sources=citations[:5],
    )
