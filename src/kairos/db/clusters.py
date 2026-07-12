"""Cluster repository."""

from __future__ import annotations

from typing import Any

from kairos.db.engine import doc_dumps, doc_loads, run, vector_param, vector_supported


async def ensure_cluster_indexes() -> None:
    """Schema (tables + indexes) is created by the engine — nothing to do."""


async def replace_all_clusters(clusters: list[dict[str, Any]]) -> int:
    """Replace cluster catalog with a fresh HDBSCAN pass."""

    def _task(conn: Any) -> int:
        conn.execute("DELETE FROM clusters")
        vectors_ok = vector_supported(conn)
        for cluster in clusters:
            centroid = cluster.get("centroid_embedding")
            if centroid and vectors_ok:
                conn.execute(
                    """
                    INSERT INTO clusters (cluster_id, name, member_count, centroid_embedding, doc)
                    VALUES (?, ?, ?, vector32(?), ?)
                    """,
                    (
                        cluster.get("cluster_id"),
                        cluster.get("name"),
                        int(cluster.get("member_count") or 0),
                        vector_param(centroid),
                        doc_dumps(cluster),
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO clusters (cluster_id, name, member_count, centroid_embedding, doc)
                    VALUES (?, ?, ?, NULL, ?)
                    """,
                    (
                        cluster.get("cluster_id"),
                        cluster.get("name"),
                        int(cluster.get("member_count") or 0),
                        doc_dumps(cluster),
                    ),
                )
        return len(clusters)

    return await run(_task)


async def get_cluster_by_id(cluster_id: str) -> dict[str, Any] | None:
    def _task(conn: Any) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT doc FROM clusters WHERE cluster_id = ?", (cluster_id,)
        ).fetchone()
        return doc_loads(row[0]) if row else None

    return await run(_task)


async def list_clusters(*, limit: int = 50) -> list[dict[str, Any]]:
    return await run(
        lambda conn: [
            doc_loads(row[0])
            for row in conn.execute(
                """
                SELECT doc FROM clusters
                ORDER BY member_count DESC, name ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
    )
