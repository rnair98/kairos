"""libSQL native vector search with in-memory cosine fallback.

Scores are raw cosine similarity (1 − cosine distance), matching the in-memory
fallback path — unlike Atlas, both paths now share one scale.
"""

from __future__ import annotations

import logging
from typing import Any

from kairos.config import settings
from kairos.db.engine import doc_loads, run, vector_param, vector_supported
from kairos.embeddings.similarity import cosine_similarity

logger = logging.getLogger(__name__)


async def ensure_vector_indexes() -> None:
    """Exact scans are sub-ms at this corpus size — no ANN index needed yet."""


async def search_clusters_by_vector(
    query_vector: list[float],
    *,
    limit: int = 50,
    exclude_cluster_ids: set[str] | None = None,
) -> list[tuple[dict[str, Any], float]] | None:
    """Return ranked (cluster, vector_score) via libSQL, or None to fallback."""
    if not settings.vector_search_enabled:
        return None
    exclude = list(exclude_cluster_ids or [])

    def _task(conn: Any) -> list[tuple[dict[str, Any], float]] | None:
        if not vector_supported(conn):
            return None
        where = "WHERE centroid_embedding IS NOT NULL"
        params: list[Any] = [vector_param(query_vector)]
        if exclude:
            placeholders = ",".join("?" for _ in exclude)
            where += f" AND cluster_id NOT IN ({placeholders})"
            params.extend(exclude)
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT doc, vector_distance_cos(centroid_embedding, vector32(?)) AS dist
            FROM clusters
            {where}
            ORDER BY dist ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [(doc_loads(row[0]), 1.0 - float(row[1])) for row in rows]

    try:
        results = await run(_task)
        return results or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Cluster vector search unavailable, using fallback: %s", exc)
        return None


async def search_bookmarks_by_vector(
    query_vector: list[float],
    *,
    limit: int = 5,
) -> list[tuple[dict[str, Any], float]] | None:
    """Return ranked (bookmark, vector_score) via libSQL, or None to fallback."""
    if not settings.vector_search_enabled:
        return None

    def _task(conn: Any) -> list[tuple[dict[str, Any], float]] | None:
        if not vector_supported(conn):
            return None
        rows = conn.execute(
            """
            SELECT doc, vector_distance_cos(embedding, vector32(?)) AS dist
            FROM bookmarks
            WHERE embedding IS NOT NULL
            ORDER BY dist ASC
            LIMIT ?
            """,
            (vector_param(query_vector), limit),
        ).fetchall()
        return [(doc_loads(row[0]), 1.0 - float(row[1])) for row in rows]

    try:
        results = await run(_task)
        return results or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Bookmark vector search unavailable, using fallback: %s", exc)
        return None


def rank_clusters_in_memory(
    query_vector: list[float],
    clusters: list[dict[str, Any]],
    *,
    limit: int | None = None,
) -> list[tuple[dict[str, Any], float]]:
    """Cosine rank clusters already loaded from the database."""
    scored: list[tuple[dict[str, Any], float]] = []
    for cluster in clusters:
        centroid = cluster.get("centroid_embedding")
        if not centroid:
            continue
        score = cosine_similarity(query_vector, centroid)
        scored.append((cluster, score))
    scored.sort(key=lambda row: row[1], reverse=True)
    if limit is not None:
        return scored[:limit]
    return scored
