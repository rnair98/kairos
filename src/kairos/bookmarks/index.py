"""Embed and cluster bookmarks stored in the database."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import numpy as np

from kairos.db.bookmarks import apply_embeddings_batch, assign_clusters_bulk, list_all_bookmarks
from kairos.db.clusters import ensure_cluster_indexes, list_clusters, replace_all_clusters
from kairos.db.engine import close_db
from kairos.embeddings.encoder import bookmark_embed_text, encode_documents, effective_embedding_model
from kairos.embeddings.similarity import cosine_similarity
from kairos.bookmarks.fingerprints import embed_fingerprint
from kairos.config import settings

logger = logging.getLogger(__name__)


@dataclass
class EmbedResult:
    total: int = 0
    embedded: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ClusterResult:
    total_bookmarks: int = 0
    clustered: int = 0
    noise: int = 0
    clusters: int = 0
    cluster_summaries: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _needs_embedding(doc: dict[str, Any], *, force: bool) -> bool:
    if force:
        return True
    if not doc.get("embedding"):
        return True
    if doc.get("embedding_model") != effective_embedding_model():
        return True
    stored = doc.get("embed_fingerprint")
    if not stored:
        return True
    return stored != embed_fingerprint(doc)


async def embed_stored_bookmarks(
    *,
    limit: int | None = None,
    force: bool = False,
) -> EmbedResult:
    """Compute and persist embeddings for the database bookmarks."""
    result = EmbedResult()
    try:
        docs = await list_all_bookmarks(limit=limit)
        result.total = len(docs)

        pending: list[tuple[str, str, str]] = []
        for doc in docs:
            x_tweet_id = doc.get("x_tweet_id")
            if not x_tweet_id:
                result.skipped += 1
                continue
            if not _needs_embedding(doc, force=force):
                result.skipped += 1
                continue
            text = bookmark_embed_text(doc)
            if not text:
                result.skipped += 1
                continue
            pending.append((x_tweet_id, text, embed_fingerprint(doc)))

        if not pending:
            return result

        texts = [text for _, text, _ in pending]
        vectors = await asyncio.to_thread(encode_documents, texts)
        updates = [
            (x_tweet_id, vector, fingerprint)
            for (x_tweet_id, _, fingerprint), vector in zip(pending, vectors, strict=True)
        ]
        written = await apply_embeddings_batch(updates)
        result.embedded = written
        logger.info(
            "Embedded %s bookmarks with %s (dim=%s)",
            written,
            effective_embedding_model(),
            len(vectors[0]) if vectors else 0,
        )
    finally:
        await close_db()

    return result


def _cluster_name(members: list[dict[str, Any]]) -> str:
    tag_counts: Counter[str] = Counter()
    for doc in members:
        tag_counts.update(doc.get("topic_tags") or [])
    if tag_counts:
        top = [tag for tag, _ in tag_counts.most_common(2)]
        return " · ".join(top)
    preview = (members[0].get("raw_text") or "").replace("\n", " ")[:40]
    return preview or f"Cluster ({len(members)})"


def _cluster_summary(members: list[dict[str, Any]]) -> str:
    previews = [
        (doc.get("raw_text") or "").replace("\n", " ")[:120]
        for doc in members[:3]
        if doc.get("raw_text")
    ]
    if not previews:
        return f"{len(members)} bookmarks grouped by embedding similarity."
    return " ".join(previews)


async def _label_clusters_parallel(
    grouped_items: list[tuple[int, list[dict[str, Any]]]],
) -> list[tuple[str, str, bool]]:
    """Label clusters concurrently (LLM or heuristic fallback)."""
    sem = asyncio.Semaphore(settings.cluster_label_concurrency)

    async def one(members: list[dict[str, Any]]) -> tuple[str, str, bool]:
        async with sem:
            if settings.cluster_naming_use_llm and settings.gemini_api_key:
                from kairos.llm.generation import label_cluster

                try:
                    llm_label = await asyncio.to_thread(label_cluster, members)
                    return llm_label.name, llm_label.summary, llm_label.evergreen
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LLM cluster label failed: %s", exc)
            return _cluster_name(members), _cluster_summary(members), False

    return await asyncio.gather(*[one(members) for _, members in grouped_items])


def _match_existing_cluster_id(
    centroid: list[float],
    existing: list[dict[str, Any]],
    *,
    threshold: float,
) -> str | None:
    """Reuse prior cluster_id when centroid is similar enough (preserves bandit params)."""
    best_id: str | None = None
    best_score = 0.0
    for cluster in existing:
        old = cluster.get("centroid_embedding")
        cid = cluster.get("cluster_id")
        if not old or not cid:
            continue
        score = cosine_similarity(centroid, old)
        if score >= threshold and score > best_score:
            best_score = score
            best_id = cid
    return best_id


async def cluster_stored_bookmarks(
    *,
    min_cluster_size: int | None = None,
) -> ClusterResult:
    """Run HDBSCAN on bookmark embeddings and persist cluster assignments."""
    result = ClusterResult()
    try:
        await ensure_cluster_indexes()
        docs = await list_all_bookmarks()
        embedded = [doc for doc in docs if doc.get("embedding")]
        result.total_bookmarks = len(docs)

        if len(embedded) < 3:
            result.errors.append("Need at least 3 embedded bookmarks to cluster")
            return result

        labels, cluster_docs = await asyncio.to_thread(
            _run_hdbscan,
            embedded,
            min_cluster_size or settings.hdbscan_min_cluster_size,
            settings.hdbscan_min_samples,
        )

        existing_clusters = await list_clusters(limit=500)
        reuse_threshold = settings.cluster_id_reuse_threshold

        now = datetime.now(timezone.utc)
        cluster_records: list[dict[str, Any]] = []
        label_to_cluster_id: dict[int, str] = {}
        grouped_items = sorted(cluster_docs.items())

        label_results = await _label_clusters_parallel(grouped_items)

        for (label, members), resolved in zip(grouped_items, label_results, strict=True):
            name, summary, evergreen = resolved
            centroid = np.mean([doc["embedding"] for doc in members], axis=0).tolist()
            cluster_id = _match_existing_cluster_id(
                centroid,
                existing_clusters,
                threshold=reuse_threshold,
            )
            if not cluster_id:
                cluster_id = str(uuid4())
            label_to_cluster_id[label] = cluster_id
            cluster_records.append(
                {
                    "cluster_id": cluster_id,
                    "name": name,
                    "summary": summary,
                    "evergreen": evergreen,
                    "centroid_embedding": centroid,
                    "member_count": len(members),
                    "last_updated": now,
                    "embedding_model": effective_embedding_model(),
                }
            )
            result.cluster_summaries.append(
                {
                    "cluster_id": cluster_id,
                    "name": cluster_records[-1]["name"],
                    "member_count": len(members),
                    "sample_x_tweet_ids": [m.get("x_tweet_id") for m in members[:3]],
                }
            )

        await replace_all_clusters(cluster_records)
        result.clusters = len(cluster_records)

        assignments: list[tuple[str, str | None]] = []
        clustered = 0
        noise = 0
        for doc, label in zip(embedded, labels, strict=True):
            x_tweet_id = doc.get("x_tweet_id")
            if not x_tweet_id:
                continue
            if label == -1:
                noise += 1
                assignments.append((x_tweet_id, None))
                continue
            cluster_id = label_to_cluster_id[label]
            assignments.append((x_tweet_id, cluster_id))
            clustered += 1

        if assignments:
            await assign_clusters_bulk(assignments)

        result.clustered = clustered
        result.noise = noise
        logger.info(
            "Clustered %s bookmarks into %s clusters (%s noise)",
            clustered,
            result.clusters,
            noise,
        )
    finally:
        await close_db()

    return result


def _run_hdbscan(
    docs: list[dict[str, Any]],
    min_cluster_size: int,
    min_samples: int,
) -> tuple[list[int], dict[int, list[dict[str, Any]]]]:
    import hdbscan

    matrix = np.array([doc["embedding"] for doc in docs], dtype=np.float32)
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
        metric="euclidean",
    )
    labels = clusterer.fit_predict(matrix).tolist()

    grouped: dict[int, list[dict[str, Any]]] = {}
    for doc, label in zip(docs, labels, strict=True):
        if label == -1:
            continue
        grouped.setdefault(label, []).append(doc)

    return labels, grouped


async def fetch_cluster_catalog() -> list[dict[str, Any]]:
    try:
        return await list_clusters()
    finally:
        await close_db()
