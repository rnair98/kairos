"""Persisted ingest cursors and sync metadata."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kairos.db.engine import doc_dumps, doc_loads, run


async def get_sync_state(source: str = "x_bookmarks") -> dict[str, Any]:
    def _task(conn: Any) -> dict[str, Any]:
        row = conn.execute(
            "SELECT doc FROM sync_state WHERE source = ?", (source,)
        ).fetchone()
        if not row:
            return {"source": source, "last_sync_at": None, "last_pages": 0, "last_fetched": 0}
        return doc_loads(row[0])

    return await run(_task)


async def update_sync_state(
    source: str,
    *,
    pages: int,
    fetched: int,
    incremental: bool,
    stopped_early: bool,
    stop_reason: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    doc = {
        "source": source,
        "last_sync_at": now,
        "last_pages": pages,
        "last_fetched": fetched,
        "last_incremental": incremental,
        "last_stopped_early": stopped_early,
        "last_stop_reason": stop_reason,
        "updated_at": now,
    }
    await run(
        lambda conn: conn.execute(
            """
            INSERT INTO sync_state (source, doc) VALUES (?, ?)
            ON CONFLICT (source) DO UPDATE SET doc = excluded.doc
            """,
            (source, doc_dumps(doc)),
        )
    )
