"""In-memory semantic search over stored bookmark embeddings."""

from __future__ import annotations

from kairos.db.bookmarks import list_all_bookmarks
from kairos.db.engine import close_db
from kairos.db.vector_search import search_bookmarks_by_vector
from kairos.embeddings.encoder import encode_query
from kairos.embeddings.similarity import cosine_similarity


async def search_bookmarks(
    query: str,
    *,
    limit: int = 5,
) -> list[dict]:
    """Rank bookmarks by cosine similarity to query (libSQL vector search or fallback)."""
    try:
        vector = encode_query(query)
        hits = await search_bookmarks_by_vector(vector, limit=limit)
        if hits is None:
            docs = await list_all_bookmarks()
            scored: list[tuple[dict, float]] = []
            for doc in docs:
                embedding = doc.get("embedding")
                if not embedding:
                    continue
                score = cosine_similarity(vector, embedding)
                scored.append((doc, score))
            scored.sort(key=lambda row: row[1], reverse=True)
            hits = scored[:limit]

        results: list[dict] = []
        for doc, score in hits:
            results.append(
                {
                    "x_tweet_id": doc.get("x_tweet_id"),
                    "url": doc.get("url"),
                    "raw_text": (doc.get("raw_text") or "")[:300],
                    "cluster_id": doc.get("cluster_id"),
                    "topic_tags": doc.get("topic_tags") or [],
                    "score": round(score, 4),
                }
            )
        return results
    finally:
        await close_db()
