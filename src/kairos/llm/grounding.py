"""Retrieve current web context via Exa search for LLM grounding."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kairos.config import settings
from kairos.llm.exa_client import get_exa_client
from kairos.models.schemas import UrlCitation

logger = logging.getLogger(__name__)


@dataclass
class GroundedText:
    text: str
    citations: list[UrlCitation]


_EMPTY = GroundedText(text="", citations=[])


def search_web(query: str, *, num_results: int | None = None) -> GroundedText:
    """Retrieve current web context for `query` via Exa.

    Deterministic retrieval step — the caller feeds the returned text to an LLM
    for synthesis. Fails soft: a bad key, rate limit, or empty result set
    returns empty text rather than raising, since grounding is an enrichment,
    not a required step.
    """
    if not query.strip():
        return _EMPTY
    try:
        client = get_exa_client()
        response = client.search(
            query,
            type="auto",
            num_results=num_results or settings.exa_num_results,
            contents={"text": {"maxCharacters": 1000}, "highlights": True},
        )
    except Exception:
        logger.warning("Exa search failed for query %r", query[:80], exc_info=True)
        return _EMPTY

    parts: list[str] = []
    citations: list[UrlCitation] = []
    for result in response.results:
        snippet = " ".join(result.highlights) if result.highlights else (result.text or "")
        snippet = snippet.strip()[:600]
        if not snippet:
            continue
        date_bit = f" ({result.published_date[:10]})" if result.published_date else ""
        title = result.title or result.url
        parts.append(f"[{title}]{date_bit}: {snippet}")
        citations.append(UrlCitation(url=result.url, title=result.title, cited_text=snippet[:200] or None))

    return GroundedText(text="\n\n".join(parts), citations=citations)
