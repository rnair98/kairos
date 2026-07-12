"""Backfill Gemini enrichment on bookmarks stored in the database."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kairos.bookmarks.fingerprints import enrich_source_hash
from kairos.db.bookmarks import apply_enrichments_batch, list_all_bookmarks
from kairos.db.engine import close_db
from kairos.llm.enrich_batch import EnrichmentJob, enrich_jobs_concurrent
from kairos.models.schemas import BookmarkDocument

logger = logging.getLogger(__name__)


@dataclass
class EnrichResult:
    total: int = 0
    enriched: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _needs_enrichment(doc: dict, *, force: bool) -> bool:
    if force:
        return True
    raw_text = (doc.get("raw_text") or "").strip()
    if not raw_text:
        return False
    current = enrich_source_hash(raw_text)
    stored = doc.get("enrich_source_hash")
    if stored:
        return stored != current
    return not doc.get("consumption_mode")


async def enrich_stored_bookmarks(
    *,
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    concurrency: int | None = None,
) -> EnrichResult:
    """Run Gemini enrichment on the database bookmarks (no X API re-fetch)."""
    result = EnrichResult()
    try:
        docs = await list_all_bookmarks(limit=limit)
        result.total = len(docs)

        jobs: list[EnrichmentJob] = []
        for doc in docs:
            x_tweet_id = doc.get("x_tweet_id")
            if not x_tweet_id:
                result.skipped += 1
                continue

            if not _needs_enrichment(doc, force=force):
                result.skipped += 1
                continue

            raw_text = doc.get("raw_text") or ""
            url = doc.get("url") or ""
            if not raw_text:
                result.skipped += 1
                continue

            if dry_run:
                logger.info("Would enrich %s", x_tweet_id)
                result.enriched += 1
                continue

            jobs.append(EnrichmentJob(x_tweet_id=x_tweet_id, raw_text=raw_text, url=url))

        if not jobs or dry_run:
            return result

        outcomes = await enrich_jobs_concurrent(jobs, concurrency=concurrency)
        jobs_by_id = {job.x_tweet_id: job for job in jobs}
        updates: list[tuple[str, BookmarkDocument]] = []

        for outcome in outcomes:
            if outcome.error:
                result.errors.append(f"{outcome.x_tweet_id}: {outcome.error}")
                continue
            if outcome.enrichment is None:
                result.errors.append(f"{outcome.x_tweet_id}: missing enrichment")
                continue

            job = jobs_by_id[outcome.x_tweet_id]
            enrichment = outcome.enrichment
            updates.append(
                (
                    outcome.x_tweet_id,
                    BookmarkDocument(
                        x_tweet_id=outcome.x_tweet_id,
                        url=job.url,
                        raw_text=job.raw_text,
                        topic_tags=enrichment.topic_tags,
                        consumption_mode=enrichment.consumption_mode,
                        energy_cost=enrichment.energy_cost,
                        geo_anchor=enrichment.geo_anchor,
                        perishability=enrichment.perishability,
                    ),
                )
            )

        written = await apply_enrichments_batch(updates)
        result.enriched = written
        if written < len(updates):
            missing = len(updates) - written
            result.errors.append(f"{missing} bookmark(s) not found during database write")

        logger.info("Enriched %s bookmark(s) with concurrency=%s", written, concurrency)
    finally:
        await close_db()

    return result
