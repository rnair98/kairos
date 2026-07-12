"""Notification persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kairos.db.engine import doc_dumps, doc_loads, run
from kairos.models.schemas import NotificationRecord, NotificationStatus, SurfaceDecision


async def ensure_notification_indexes() -> None:
    """Schema (tables + indexes) is created by the engine — nothing to do."""


async def save_notification(
    decision: SurfaceDecision,
    *,
    user_id: str | None = None,
) -> NotificationRecord:
    """Persist a surface event."""
    from kairos.db.bandit import bandit_user_id

    record = NotificationRecord(
        user_id=bandit_user_id(user_id),
        cluster_id=decision.cluster_id,
        digest=decision.digest,
        context_snapshot=decision.context,
    )
    payload = record.model_dump(mode="json")

    await run(
        lambda conn: conn.execute(
            """
            INSERT INTO notifications (notification_id, user_id, status, created_at, doc)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (notification_id) DO UPDATE SET
                user_id = excluded.user_id,
                status = excluded.status,
                created_at = excluded.created_at,
                doc = excluded.doc
            """,
            (
                payload.get("notification_id"),
                payload.get("user_id"),
                payload.get("status"),
                payload.get("created_at"),
                doc_dumps(payload),
            ),
        )
    )
    return record


async def get_notification(notification_id: str) -> NotificationRecord | None:
    def _task(conn: Any) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT doc FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        return doc_loads(row[0]) if row else None

    doc = await run(_task)
    if not doc:
        return None
    return NotificationRecord.model_validate(doc)


async def update_notification_status(
    notification_id: str,
    status: NotificationStatus,
) -> bool:
    now = datetime.now(timezone.utc)

    def _task(conn: Any) -> bool:
        row = conn.execute(
            "SELECT doc FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        if not row:
            return False
        doc = doc_loads(row[0])
        doc["status"] = status
        doc["updated_at"] = now
        conn.execute(
            "UPDATE notifications SET status = ?, doc = ? WHERE notification_id = ?",
            (status, doc_dumps(doc), notification_id),
        )
        return True

    return await run(_task)


async def list_notifications(
    *,
    limit: int = 20,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    from kairos.db.bandit import bandit_user_id

    uid = bandit_user_id(user_id)
    if uid == "__default__":
        where = "user_id = ? OR user_id IS NULL"
    else:
        where = "user_id = ?"

    return await run(
        lambda conn: [
            doc_loads(row[0])
            for row in conn.execute(
                f"""
                SELECT doc FROM notifications
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (uid, limit),
            ).fetchall()
        ]
    )
