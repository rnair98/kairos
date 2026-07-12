"""Feedback event persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from kairos.config import settings
from kairos.db.bandit import bandit_user_id
from kairos.db.engine import doc_dumps, doc_loads, iso, run
from kairos.models.schemas import ContextSnapshot, FeedbackAction


async def ensure_feedback_indexes() -> None:
    """Schema (tables + indexes) is created by the engine — nothing to do."""


def _insert_doc_sync(conn: Any, doc: dict[str, Any]) -> None:
    has_snooze = any(e.get("type") == "snoozed" for e in doc.get("events") or [])
    created_at = doc.get("created_at")
    conn.execute(
        """
        INSERT INTO feedback_events
            (event_id, notification_id, user_id, cluster_id, context_class,
             derived_reward, has_snooze, sim, run_id, persona, created_at, doc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc["event_id"],
            doc.get("notification_id"),
            doc.get("user_id"),
            doc.get("cluster_id"),
            doc.get("context_class"),
            doc.get("derived_reward"),
            1 if has_snooze else 0,
            1 if doc.get("sim") else 0,
            doc.get("run_id"),
            doc.get("persona"),
            iso(created_at) if isinstance(created_at, datetime) else created_at,
            doc_dumps(doc),
        ),
    )


async def insert_feedback_event(
    *,
    notification_id: str,
    cluster_id: str,
    context_class: str,
    context_snapshot: ContextSnapshot,
    action: FeedbackAction,
    derived_reward: float | None,
    notification_text: str,
    url: str | None = None,
    user_id: str | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    event_id = str(uuid4())
    events: list[dict[str, Any]] = [{"type": "shown", "t": 0}]
    payload: dict[str, Any] = {"type": action, "t": 0}
    if url:
        payload["url"] = url
    events.append(payload)

    doc = {
        "event_id": event_id,
        "notification_id": notification_id,
        "user_id": bandit_user_id(user_id),
        "cluster_id": cluster_id,
        "context_class": context_class,
        "context_snapshot": context_snapshot.model_dump(),
        "notification_text": notification_text,
        "events": events,
        "derived_reward": derived_reward,
        "snooze_context": context_snapshot.model_dump() if action == "snoozed" else None,
        "created_at": now,
    }
    await run(lambda conn: _insert_doc_sync(conn, doc))
    return event_id


async def insert_sim_feedback_event(doc: dict[str, Any]) -> None:
    """Write a sim-tagged feedback event assembled by the gym."""
    await run(lambda conn: _insert_doc_sync(conn, doc))


async def delete_sim_feedback(run_id: str | None = None) -> int:
    """Delete sim-tagged feedback events, optionally scoped to one gym run."""

    def _task(conn: Any) -> int:
        if run_id:
            rows = conn.execute(
                "DELETE FROM feedback_events WHERE sim = 1 AND run_id = ? RETURNING event_id",
                (run_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "DELETE FROM feedback_events WHERE sim = 1 RETURNING event_id"
            ).fetchall()
        return len(rows)

    return await run(_task)


async def list_feedback_sample(
    *,
    limit: int,
    days: int = 14,
    exclude_sim: bool = False,
) -> list[dict[str, Any]]:
    """Recent scored events with notification text — GEPA training sample."""
    since = iso(datetime.now(timezone.utc) - timedelta(days=days))
    sim_clause = "AND sim != 1" if exclude_sim else ""

    def _task(conn: Any) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"""
            SELECT doc FROM feedback_events
            WHERE created_at >= ?
              AND derived_reward IS NOT NULL
              AND json_extract(doc, '$.notification_text') IS NOT NULL
              AND json_extract(doc, '$.notification_text') != ''
              {sim_clause}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (since, limit),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for (doc_text,) in rows:
            doc = doc_loads(doc_text)
            out.append(
                {
                    "notification_text": doc.get("notification_text"),
                    "derived_reward": doc.get("derived_reward"),
                    "digest_style": doc.get("digest_style"),
                }
            )
        return out

    return await run(_task)


async def list_snoozed_cluster_ids(
    context_class: str,
    *,
    user_id: str | None = None,
) -> list[str]:
    """Cluster IDs snoozed for this user × context bucket within the TTL window."""
    since = iso(datetime.now(timezone.utc) - timedelta(minutes=settings.snooze_ttl_minutes))
    uid = bandit_user_id(user_id)

    def _task(conn: Any) -> list[str]:
        rows = conn.execute(
            """
            SELECT DISTINCT cluster_id FROM feedback_events
            WHERE user_id = ? AND context_class = ? AND has_snooze = 1
              AND created_at >= ? AND cluster_id IS NOT NULL AND cluster_id != ''
            LIMIT 100
            """,
            (uid, context_class, since),
        ).fetchall()
        return [row[0] for row in rows]

    return await run(_task)
