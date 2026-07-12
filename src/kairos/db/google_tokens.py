"""Per-user Google OAuth token storage."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kairos.db.engine import doc_dumps, doc_loads, iso, run


async def ensure_google_indexes() -> None:
    """Schema (tables + indexes) is created by the engine — nothing to do."""


def _load_sync(conn: Any, user_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT doc FROM google_connections WHERE user_id = ?", (user_id,)
    ).fetchone()
    return doc_loads(row[0]) if row else None


def _save_sync(conn: Any, user_id: str, doc: dict[str, Any]) -> None:
    updated_at = doc.get("updated_at")
    conn.execute(
        """
        INSERT INTO google_connections (user_id, email, updated_at, doc)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET
            email = excluded.email,
            updated_at = excluded.updated_at,
            doc = excluded.doc
        """,
        (
            user_id,
            doc.get("email"),
            iso(updated_at) if isinstance(updated_at, datetime) else updated_at,
            doc_dumps(doc),
        ),
    )


async def save_google_connection(
    *,
    user_id: str,
    email: str,
    access_token: str,
    refresh_token: str,
    scopes: list[str],
    token_expiry: datetime | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "scopes": scopes,
        "token_expiry": token_expiry,
        "updated_at": now,
        "created_at": now,
    }

    def _task(conn: Any) -> None:
        existing = _load_sync(conn, user_id)
        if existing:
            payload["created_at"] = existing.get("created_at", now)
        _save_sync(conn, user_id, payload)

    await run(_task)


async def load_google_connection(user_id: str) -> dict[str, Any] | None:
    return await run(lambda conn: _load_sync(conn, user_id))


async def update_google_tokens(
    user_id: str,
    *,
    access_token: str,
    token_expiry: datetime | None = None,
) -> None:
    def _task(conn: Any) -> None:
        doc = _load_sync(conn, user_id)
        if doc is None:
            return
        doc["access_token"] = access_token
        doc["updated_at"] = datetime.now(timezone.utc)
        if token_expiry is not None:
            doc["token_expiry"] = token_expiry
        _save_sync(conn, user_id, doc)

    await run(_task)


async def delete_google_connection(user_id: str) -> bool:
    def _task(conn: Any) -> bool:
        rows = conn.execute(
            "DELETE FROM google_connections WHERE user_id = ? RETURNING user_id",
            (user_id,),
        ).fetchall()
        return bool(rows)

    return await run(_task)


async def list_google_connections(limit: int = 50) -> list[dict[str, Any]]:
    return await run(
        lambda conn: [
            doc_loads(row[0])
            for row in conn.execute(
                "SELECT doc FROM google_connections ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]
    )
