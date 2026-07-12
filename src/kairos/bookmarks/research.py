"""Backfill upfront web research on bookmarks stored in the database."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from kairos.bookmarks.fingerprints import research_source_hash
from kairos.bookmarks.link_fetch import (
    fetch_link_preview_for_bookmark,
    link_content_text,
    needs_link_fetch,
)
from kairos.bookmarks.urls import compose_research_input
from kairos.config import settings
from kairos.db.bookmarks import apply_link_preview, apply_research_batch, list_bookmarks_for_research
from kairos.db.engine import close_db
from kairos.llm.research import research_bookmark
from kairos.models.schemas import BookmarkResearch

logger = logging.getLogger(__name__)


@dataclass
class ResearchResult:
    total: int = 0
    researched: int = 0
    link_fetched: int = 0
    skipped: int = 0
    fast_path: int = 0
    errors: list[str] = field(default_factory=list)


def _needs_research(doc: dict, *, force: bool) -> bool:
    if force:
        return True
    raw_text = (doc.get("raw_text") or "").strip()
    url = (doc.get("url") or "").strip()
    if not raw_text and not url:
        return False
    link_content = link_content_text(doc)
    current = research_source_hash(raw_text, url, link_content)
    stored = doc.get("research_source_hash")
    if stored:
        return stored != current
    return not doc.get("research_summary")


def _merge_link_preview(doc: dict, preview_fields: dict[str, Any]) -> dict:
    return {**doc, **preview_fields}


def _use_fast_path(doc: dict) -> bool:
    if not settings.research_fast_mode:
        return False
    return len(link_content_text(doc)) >= settings.research_min_link_chars_for_fast


async def research_stored_bookmarks(
    *,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    concurrency: int | None = None,
    clustered_only: bool | None = None,
) -> ResearchResult:
    """Fetch linked pages, then run Gemini research (no X API re-fetch)."""
    result = ResearchResult()
    gemini_limit = concurrency if concurrency is not None else settings.research_concurrency
    gemini_sem = asyncio.Semaphore(max(1, gemini_limit))
    link_sem = asyncio.Semaphore(max(1, settings.link_fetch_concurrency))
    only_clustered = (
        settings.research_clustered_only if clustered_only is None else clustered_only
    )

    async def _one(doc: dict) -> tuple[str, BookmarkResearch | None, str, str | None, bool, bool]:
        x_tweet_id = doc["x_tweet_id"]
        working = dict(doc)
        fetched = False

        if needs_link_fetch(working, force=force):
            async with link_sem:
                preview = await asyncio.to_thread(fetch_link_preview_for_bookmark, working)
            if preview:
                fields = preview.as_doc_fields()
                await apply_link_preview(x_tweet_id, fields)
                working = _merge_link_preview(working, fields)
                fetched = preview.ok

        raw_text = (working.get("raw_text") or "").strip()
        url = working.get("url") or ""
        context = compose_research_input(working)
        source_hash = research_source_hash(raw_text, url, link_content_text(working))
        fast = _use_fast_path(working)
        async with gemini_sem:
            try:
                research = await asyncio.to_thread(
                    research_bookmark,
                    context,
                    url,
                    skip_google_search=fast,
                )
                return x_tweet_id, research, source_hash, None, fetched, fast
            except Exception as exc:  # noqa: BLE001
                return x_tweet_id, None, source_hash, str(exc), fetched, fast

    try:
        docs = await list_bookmarks_for_research(limit=limit, clustered_only=only_clustered)
        result.total = len(docs)

        tasks = []
        for doc in docs:
            x_tweet_id = doc.get("x_tweet_id")
            raw_text = (doc.get("raw_text") or "").strip()
            url = doc.get("url") or ""
            if not x_tweet_id or (not raw_text and not url):
                result.skipped += 1
                continue
            if not _needs_research(doc, force=force) and not needs_link_fetch(doc, force=force):
                result.skipped += 1
                continue
            if dry_run:
                logger.info("Would research %s", x_tweet_id)
                result.researched += 1
                continue
            tasks.append(_one(doc))

        if not tasks:
            if result.skipped:
                print(
                    f"  → all {result.skipped} bookmark(s) already researched — nothing to do",
                    flush=True,
                )
            return result

        if dry_run:
            return result

        total_tasks = len(tasks)
        print(
            f"  → {total_tasks} bookmark(s) to research "
            f"(concurrency={gemini_limit}"
            f"{', clustered only' if only_clustered else ''})…",
            flush=True,
        )

        updates: list[tuple[str, BookmarkResearch, str]] = []
        done = 0
        for coro in asyncio.as_completed([asyncio.create_task(t) for t in tasks]):
            x_tweet_id, research, source_hash, error, fetched, fast = await coro
            done += 1
            if fetched:
                result.link_fetched += 1
            if fast:
                result.fast_path += 1
            if error or research is None:
                result.errors.append(f"{x_tweet_id}: {error or 'no research'}")
                print(f"  [{done}/{total_tasks}] {x_tweet_id} ✗ {error or 'failed'}", flush=True)
                continue
            updates.append((x_tweet_id, research, source_hash))
            mode = "fast" if fast else "grounded"
            summary = (research.research_summary or "")[:60].replace("\n", " ")
            print(f"  [{done}/{total_tasks}] {x_tweet_id} ✓ {mode} — {summary}…", flush=True)

        written = await apply_research_batch(updates)
        result.researched = written
        if written < len(updates):
            result.errors.append(f"{len(updates) - written} bookmark(s) not found during write")

        logger.info(
            "Researched %s bookmark(s), fetched %s link(s), fast_path=%s, concurrency=%s",
            written,
            result.link_fetched,
            result.fast_path,
            gemini_limit,
        )
    finally:
        await close_db()

    return result
