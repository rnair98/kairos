"""Persisted pipeline events — shared log for SSE across processes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from kairos.config import settings
from kairos.db.engine import iso, run
from kairos.observability.bus import AgentEvent


def _cutoff() -> str:
    return iso(datetime.now(timezone.utc) - timedelta(days=settings.event_persist_ttl_days))


async def ensure_event_indexes() -> None:
    """Schema (tables + indexes) is created by the engine — nothing to do."""


async def insert_pipeline_event(event: AgentEvent) -> None:
    if not settings.event_persist_enabled:
        return
    cutoff = _cutoff()

    def _task(conn: Any) -> None:
        # TTL emulation — expire old rows on write.
        conn.execute("DELETE FROM pipeline_events WHERE timestamp < ?", (cutoff,))
        conn.execute(
            "INSERT INTO pipeline_events (timestamp, kind, message, data) VALUES (?, ?, ?, ?)",
            (
                iso(event.timestamp),
                event.kind,
                event.message,
                json.dumps(event.data or {}, ensure_ascii=False, default=str),
            ),
        )

    await run(_task)


async def list_recent_events(*, limit: int = 500) -> list[AgentEvent]:
    if not settings.event_persist_enabled:
        return []
    cutoff = _cutoff()

    def _task(conn: Any) -> list[tuple]:
        return conn.execute(
            """
            SELECT timestamp, kind, message, data FROM pipeline_events
            WHERE timestamp >= ?
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()

    rows = await run(_task)
    events: list[AgentEvent] = []
    for ts_text, kind, message, data_text in rows:
        try:
            ts = datetime.fromisoformat(ts_text)
        except (TypeError, ValueError):
            ts = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            data = json.loads(data_text) if data_text else {}
        except ValueError:
            data = {}
        events.append(
            AgentEvent(
                timestamp=ts,
                kind=str(kind or "unknown"),
                message=str(message or ""),
                data=dict(data or {}),
            )
        )
    return events
