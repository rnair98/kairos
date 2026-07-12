"""Bookmark repository — upsert by x_tweet_id."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kairos.bookmarks.fingerprints import enrich_source_hash
from kairos.db.engine import (
    doc_dumps,
    doc_loads,
    iso,
    run,
    vector_param,
    vector_supported,
)
from kairos.embeddings.encoder import effective_embedding_model
from kairos.models.schemas import BookmarkDocument, BookmarkResearch

DERIVED_FIELDS_ON_TEXT_CHANGE = (
    "embedding",
    "cluster_id",
    "embed_fingerprint",
    "embedding_model",
    "enrich_source_hash",
    "topic_tags",
    "consumption_mode",
    "energy_cost",
    "geo_anchor",
    "perishability",
    "link_final_url",
    "link_title",
    "link_description",
    "link_body_excerpt",
    "link_fetched_at",
    "link_fetch_error",
    "research_summary",
    "relevance_signal",
    "relevance_status",
    "research_sources",
    "researched_at",
    "research_source_hash",
)


async def ensure_bookmark_indexes() -> None:
    """Schema (tables + indexes) is created by the engine — nothing to do."""


def _hot_fields(doc: dict[str, Any]) -> tuple[Any, Any, Any, Any, int]:
    embedding = doc.get("embedding")
    return (
        doc.get("cluster_id"),
        iso(doc["ingested_at"]) if isinstance(doc.get("ingested_at"), datetime) else doc.get("ingested_at"),
        iso(doc["tweet_created_at"]) if isinstance(doc.get("tweet_created_at"), datetime) else doc.get("tweet_created_at"),
        iso(doc["last_synced_at"]) if isinstance(doc.get("last_synced_at"), datetime) else doc.get("last_synced_at"),
        1 if embedding else 0,
    )


def _write_doc_sync(conn: Any, x_tweet_id: str, doc: dict[str, Any]) -> None:
    cluster_id, ingested_at, tweet_created_at, last_synced_at, has_embedding = _hot_fields(doc)
    embedding = doc.get("embedding")
    if embedding and vector_supported(conn):
        conn.execute(
            """
            INSERT INTO bookmarks (x_tweet_id, cluster_id, ingested_at, tweet_created_at,
                                   last_synced_at, has_embedding, embedding, doc)
            VALUES (?, ?, ?, ?, ?, ?, vector32(?), ?)
            ON CONFLICT (x_tweet_id) DO UPDATE SET
                cluster_id = excluded.cluster_id,
                ingested_at = excluded.ingested_at,
                tweet_created_at = excluded.tweet_created_at,
                last_synced_at = excluded.last_synced_at,
                has_embedding = excluded.has_embedding,
                embedding = excluded.embedding,
                doc = excluded.doc
            """,
            (
                x_tweet_id,
                cluster_id,
                ingested_at,
                tweet_created_at,
                last_synced_at,
                has_embedding,
                vector_param(embedding),
                doc_dumps(doc),
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO bookmarks (x_tweet_id, cluster_id, ingested_at, tweet_created_at,
                                   last_synced_at, has_embedding, embedding, doc)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT (x_tweet_id) DO UPDATE SET
                cluster_id = excluded.cluster_id,
                ingested_at = excluded.ingested_at,
                tweet_created_at = excluded.tweet_created_at,
                last_synced_at = excluded.last_synced_at,
                has_embedding = excluded.has_embedding,
                embedding = excluded.embedding,
                doc = excluded.doc
            """,
            (
                x_tweet_id,
                cluster_id,
                ingested_at,
                tweet_created_at,
                last_synced_at,
                has_embedding,
                doc_dumps(doc),
            ),
        )


def _load_doc_sync(conn: Any, x_tweet_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT doc FROM bookmarks WHERE x_tweet_id = ?", (x_tweet_id,)
    ).fetchone()
    return doc_loads(row[0]) if row else None


def _merge_fields_sync(conn: Any, x_tweet_id: str, fields: dict[str, Any]) -> bool:
    """Merge-update helper: merge fields into the stored doc. Returns matched."""
    doc = _load_doc_sync(conn, x_tweet_id)
    if doc is None:
        return False
    doc.update(fields)
    _write_doc_sync(conn, x_tweet_id, doc)
    return True


async def get_by_x_tweet_id(x_tweet_id: str) -> dict[str, Any] | None:
    return await run(lambda conn: _load_doc_sync(conn, x_tweet_id))


async def count_bookmarks() -> int:
    return await run(
        lambda conn: int(conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0])
    )


async def list_bookmarks(
    *,
    limit: int = 20,
    skip: int = 0,
) -> list[dict[str, Any]]:
    """Return bookmarks newest-first by ingested_at, then tweet_created_at."""
    return await run(
        lambda conn: [
            doc_loads(row[0])
            for row in conn.execute(
                """
                SELECT doc FROM bookmarks
                ORDER BY ingested_at DESC, tweet_created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, skip),
            ).fetchall()
        ]
    )


async def list_bookmarks_by_cluster(
    cluster_id: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return bookmarks assigned to a cluster."""
    return await run(
        lambda conn: [
            doc_loads(row[0])
            for row in conn.execute(
                """
                SELECT doc FROM bookmarks
                WHERE cluster_id = ?
                ORDER BY ingested_at DESC
                LIMIT ?
                """,
                (cluster_id, limit),
            ).fetchall()
        ]
    )


async def list_all_bookmarks(*, limit: int | None = None) -> list[dict[str, Any]]:
    """Return all bookmarks, optionally capped."""
    return await list_bookmarks_for_research(limit=limit, clustered_only=False)


async def list_bookmarks_for_research(
    *,
    limit: int | None = None,
    clustered_only: bool = False,
) -> list[dict[str, Any]]:
    """Bookmarks eligible for research — optionally only those assigned to clusters."""
    where = "WHERE cluster_id IS NOT NULL AND cluster_id != ''" if clustered_only else ""
    cap = limit if limit is not None else 10_000
    return await run(
        lambda conn: [
            doc_loads(row[0])
            for row in conn.execute(
                f"SELECT doc FROM bookmarks {where} ORDER BY ingested_at DESC LIMIT ?",
                (cap,),
            ).fetchall()
        ]
    )


async def count_unclustered_embedded() -> int:
    """Bookmarks with embeddings but no cluster assignment."""
    return await run(
        lambda conn: int(
            conn.execute(
                """
                SELECT COUNT(*) FROM bookmarks
                WHERE has_embedding = 1 AND (cluster_id IS NULL OR cluster_id = '')
                """
            ).fetchone()[0]
        )
    )


async def apply_enrichment(x_tweet_id: str, doc: BookmarkDocument) -> bool:
    """Write enrichment fields onto an existing bookmark."""
    now = datetime.now(timezone.utc)
    payload = doc.model_dump(
        exclude_none=True,
        include={
            "topic_tags",
            "consumption_mode",
            "energy_cost",
            "geo_anchor",
            "perishability",
        },
    )
    payload["enrich_source_hash"] = enrich_source_hash(doc.raw_text)
    payload["last_synced_at"] = now
    return await run(lambda conn: _merge_fields_sync(conn, x_tweet_id, payload))


async def apply_enrichments_batch(docs: list[tuple[str, BookmarkDocument]]) -> int:
    """Apply enrichment updates sequentially. Returns count of matched documents."""
    matched = 0
    for tid, doc in docs:
        if await apply_enrichment(tid, doc):
            matched += 1
    return matched


async def apply_link_preview(x_tweet_id: str, preview: dict[str, Any]) -> bool:
    """Persist fetched link metadata on a bookmark."""
    now = datetime.now(timezone.utc)
    payload = {
        **preview,
        "link_fetched_at": now,
    }
    return await run(lambda conn: _merge_fields_sync(conn, x_tweet_id, payload))


async def apply_research(x_tweet_id: str, research: BookmarkResearch, *, source_hash: str) -> bool:
    """Write web-research fields onto an existing bookmark."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "research_summary": research.research_summary,
        "relevance_signal": research.relevance_signal,
        "relevance_status": research.relevance_status,
        "research_sources": [c.model_dump() for c in research.research_sources],
        "researched_at": now,
        "research_source_hash": source_hash,
    }
    return await run(lambda conn: _merge_fields_sync(conn, x_tweet_id, payload))


async def apply_research_batch(
    updates: list[tuple[str, BookmarkResearch, str]],
) -> int:
    """Apply research updates sequentially. Returns count of matched documents."""
    matched = 0
    for tid, research, source_hash in updates:
        if await apply_research(tid, research, source_hash=source_hash):
            matched += 1
    return matched


async def apply_embeddings_batch(updates: list[tuple[str, list[float], str]]) -> int:
    """Bulk-write embeddings with fingerprint + model metadata."""
    if not updates:
        return 0
    now = datetime.now(timezone.utc)
    model = effective_embedding_model()

    def _task(conn: Any) -> int:
        matched = 0
        for x_tweet_id, embedding, fingerprint in updates:
            doc = _load_doc_sync(conn, x_tweet_id)
            if doc is None:
                continue
            doc["embedding"] = embedding
            doc["embed_fingerprint"] = fingerprint
            doc["embedding_model"] = model
            doc["last_synced_at"] = now
            doc.pop("cluster_id", None)
            _write_doc_sync(conn, x_tweet_id, doc)
            matched += 1
        return matched

    return await run(_task)


async def assign_clusters_bulk(assignments: list[tuple[str, str | None]]) -> int:
    """Set cluster_id (or None for noise) on many bookmarks. Returns matched count."""
    if not assignments:
        return 0

    def _task(conn: Any) -> int:
        matched = 0
        for x_tweet_id, cluster_id in assignments:
            doc = _load_doc_sync(conn, x_tweet_id)
            if doc is None:
                continue
            doc["cluster_id"] = cluster_id
            _write_doc_sync(conn, x_tweet_id, doc)
            matched += 1
        return matched

    return await run(_task)


async def upsert_bookmark(doc: BookmarkDocument) -> str:
    """Upsert bookmark by x_tweet_id. Returns 'inserted' | 'updated' | 'unchanged'."""
    now = datetime.now(timezone.utc)
    payload = doc.model_dump(exclude_none=True, exclude={"id"})
    payload["last_synced_at"] = now
    if doc.consumption_mode is not None or doc.topic_tags:
        payload["enrich_source_hash"] = enrich_source_hash(doc.raw_text)

    def _task(conn: Any) -> str:
        existing = _load_doc_sync(conn, doc.x_tweet_id)
        text_changed = bool(existing) and existing.get("raw_text") != doc.raw_text

        if existing and not text_changed and doc.embedding is None:
            existing["last_synced_at"] = now
            _write_doc_sync(conn, doc.x_tweet_id, existing)
            return "unchanged"

        if existing:
            payload.setdefault("ingested_at", existing.get("ingested_at", now))
            merged = dict(existing)
            merged.update(payload)
            if text_changed:
                for field in DERIVED_FIELDS_ON_TEXT_CHANGE:
                    merged.pop(field, None)
                    if field in payload:
                        merged[field] = payload[field]
            _write_doc_sync(conn, doc.x_tweet_id, merged)
            return "updated"

        payload.setdefault("ingested_at", now)
        payload.setdefault("surface_count", 0)
        _write_doc_sync(conn, doc.x_tweet_id, payload)
        return "inserted"

    return await run(_task)
