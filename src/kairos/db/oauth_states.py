"""Short-lived OAuth CSRF state tokens."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from kairos.db.engine import iso, run

TTL_SECONDS = 600


def _cutoff() -> str:
    return iso(datetime.now(timezone.utc) - timedelta(seconds=TTL_SECONDS))


async def ensure_oauth_state_indexes() -> None:
    """Schema (tables + indexes) is created by the engine — nothing to do."""


async def save_oauth_state(state: str) -> None:
    now = iso(datetime.now(timezone.utc))
    cutoff = _cutoff()

    def _task(conn: Any) -> None:
        conn.execute("DELETE FROM oauth_states WHERE created_at < ?", (cutoff,))
        conn.execute(
            """
            INSERT INTO oauth_states (state, created_at) VALUES (?, ?)
            ON CONFLICT (state) DO UPDATE SET created_at = excluded.created_at
            """,
            (state, now),
        )

    await run(_task)


async def consume_oauth_state(state: str) -> bool:
    cutoff = _cutoff()

    def _task(conn: Any) -> bool:
        rows = conn.execute(
            "DELETE FROM oauth_states WHERE state = ? AND created_at >= ? RETURNING state",
            (state, cutoff),
        ).fetchall()
        conn.execute("DELETE FROM oauth_states WHERE created_at < ?", (cutoff,))
        return bool(rows)

    return await run(_task)
