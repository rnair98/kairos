"""Persist fused headspace snapshots per user."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kairos.db.engine import doc_dumps, doc_loads, iso, run
from kairos.models.schemas import ContextSnapshot

LEGACY_DOC_ID = "latest"


def _doc_id(user_id: str | None) -> str:
    return user_id or LEGACY_DOC_ID


async def ensure_context_indexes() -> None:
    """Schema (tables + indexes) is created by the engine — nothing to do."""


async def save_context(snapshot: ContextSnapshot, *, user_id: str | None = None) -> None:
    doc_id = _doc_id(user_id)
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "user_id": user_id,
        "snapshot": snapshot.model_dump(mode="json"),
        "updated_at": now,
    }
    await run(
        lambda conn: conn.execute(
            """
            INSERT INTO context_cache (doc_id, user_id, updated_at, doc)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (doc_id) DO UPDATE SET
                user_id = excluded.user_id,
                updated_at = excluded.updated_at,
                doc = excluded.doc
            """,
            (doc_id, user_id, iso(now), doc_dumps(payload)),
        )
    )


async def load_context(user_id: str | None = None) -> ContextSnapshot | None:
    doc_id = _doc_id(user_id)

    def _task(conn: Any) -> ContextSnapshot | None:
        row = conn.execute(
            "SELECT doc FROM context_cache WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        doc = doc_loads(row[0]) if row else None
        if not doc or not doc.get("snapshot"):
            if user_id and doc_id != LEGACY_DOC_ID:
                legacy_row = conn.execute(
                    "SELECT doc FROM context_cache WHERE doc_id = ?", (LEGACY_DOC_ID,)
                ).fetchone()
                legacy = doc_loads(legacy_row[0]) if legacy_row else None
                if legacy and legacy.get("snapshot"):
                    return ContextSnapshot.model_validate(legacy["snapshot"])
            return None
        return ContextSnapshot.model_validate(doc["snapshot"])

    return await run(_task)
