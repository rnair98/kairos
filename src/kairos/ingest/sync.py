"""X API → the database bookmark sync orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kairos.db.bookmarks import ensure_bookmark_indexes, get_by_x_tweet_id, upsert_bookmark
from kairos.db.engine import close_db
from kairos.db.sync_state import update_sync_state
from kairos.ingest.enrich import enrich_bookmark_documents
from kairos.ingest.x.client import XApiClient, XApiError
from kairos.ingest.x.normalize import normalize_bookmark
from kairos.models.schemas import BookmarkDocument

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    enriched: int = 0
    pages: int = 0
    incremental: bool = False
    stopped_early: bool = False
    stop_reason: str | None = None
    errors: list[str] = field(default_factory=list)


async def sync_bookmarks_from_x(
    *,
    max_pages: int | None = None,
    incremental: bool = False,
    enrich: bool = False,
    enrich_existing: bool = False,
    enrich_concurrency: int | None = None,
    close_after: bool = True,
) -> SyncResult:
    """Paginate X bookmarks, normalize, upsert to the database.

    When ``incremental=True``, stop after the first page where every tweet is
    already stored with unchanged ``raw_text`` (bookmarks API returns newest first).

    Enrichment is off by default — use ``kairos bookmarks enrich`` or ``bookmarks prep``.
    """
    result = SyncResult(incremental=incremental)
    client = XApiClient()

    await ensure_bookmark_indexes()

    try:
        async for page in client.iter_bookmarks(max_pages=max_pages):
            result.pages += 1
            tweets = page.get("data") or []
            includes = page.get("includes") or {}

            page_docs: list[BookmarkDocument] = []
            page_meta: list[tuple[BookmarkDocument, bool]] = []
            page_known_unchanged = 0

            for tweet in tweets:
                result.fetched += 1
                try:
                    doc = normalize_bookmark(tweet, includes)
                    existing = await get_by_x_tweet_id(doc.x_tweet_id)
                    text_changed = not existing or existing.get("raw_text") != doc.raw_text
                    if existing and not text_changed:
                        page_known_unchanged += 1
                    needs_enrich = enrich and (not existing or text_changed or enrich_existing)
                    page_docs.append(doc)
                    page_meta.append((doc, needs_enrich and bool(doc.raw_text)))
                except Exception as exc:  # noqa: BLE001 — collect per-tweet errors, continue sync
                    msg = f"tweet {tweet.get('id')}: {exc}"
                    logger.exception("Failed to normalize bookmark tweet %s", tweet.get("id"))
                    result.errors.append(msg)

            page_inserts_before = result.inserted
            page_updates_before = result.updated

            to_enrich = [doc for doc, needs in page_meta if needs]
            if to_enrich:
                enriched_docs, enrich_errors, enriched_count = await enrich_bookmark_documents(
                    to_enrich,
                    concurrency=enrich_concurrency,
                )
                result.enriched += enriched_count
                result.errors.extend(enrich_errors)
                enriched_by_id = {doc.x_tweet_id: doc for doc in enriched_docs}
                page_docs = [enriched_by_id.get(doc.x_tweet_id, doc) for doc in page_docs]

            for doc in page_docs:
                try:
                    status = await upsert_bookmark(doc)
                    if status == "inserted":
                        result.inserted += 1
                    elif status == "updated":
                        result.updated += 1
                    else:
                        result.unchanged += 1
                except Exception as exc:  # noqa: BLE001 — collect per-tweet errors, continue sync
                    msg = f"tweet {doc.x_tweet_id}: {exc}"
                    logger.exception("Failed to upsert bookmark %s", doc.x_tweet_id)
                    result.errors.append(msg)

            page_inserts = result.inserted - page_inserts_before
            page_updates = result.updated - page_updates_before
            if (
                incremental
                and tweets
                and page_inserts == 0
                and page_updates == 0
                and page_known_unchanged == len(tweets)
            ):
                result.stopped_early = True
                result.stop_reason = "caught_up"
                logger.info(
                    "Incremental X sync stopping after page %d — %d known unchanged tweets",
                    result.pages,
                    page_known_unchanged,
                )
                break
    except XApiError:
        raise
    finally:
        await update_sync_state(
            "x_bookmarks",
            pages=result.pages,
            fetched=result.fetched,
            incremental=incremental,
            stopped_early=result.stopped_early,
            stop_reason=result.stop_reason,
        )
        if close_after:
            await close_db()

    return result


async def fetch_x_user_id() -> str:
    """Resolve authenticated user id via GET /2/users/me."""
    client = XApiClient()
    payload = await client.get_me()
    return payload["data"]["id"]
