"""libSQL (Turso) storage engine — local-first embedded database.

All db modules funnel through `run()`, which executes a synchronous closure on a
single dedicated worker thread. That gives every public db function atomicity
(read-modify-write happens as one serialized unit) and satisfies SQLite's
same-thread connection rule without holding locks across awaits.

Documents are stored as JSON in a `doc` column beside hot key columns used in
WHERE/ORDER BY. Datetimes round-trip through ISO-8601 strings via the codec
below, so lexicographic comparison in SQL matches chronological order (all UTC).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, TypeVar

from kairos.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

_conn: Any | None = None
_executor: ThreadPoolExecutor | None = None
_persist_connection: bool = False
_schema_ready: bool = False
_vector_ok: bool | None = None

_ISO_DT = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$"
)


# ---------------------------------------------------------------------------
# JSON codec — datetime round-tripping
# ---------------------------------------------------------------------------


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, str) and _ISO_DT.match(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if isinstance(value, dict):
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def doc_dumps(doc: dict[str, Any]) -> str:
    return json.dumps(_encode(doc), ensure_ascii=False)


def doc_loads(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    return _decode(json.loads(text))


def iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Connection / executor lifecycle
# ---------------------------------------------------------------------------


def _connect() -> Any:
    import libsql

    if settings.turso_database_url and settings.turso_auth_token:
        conn = libsql.connect(
            settings.kairos_db_path,
            sync_url=settings.turso_database_url,
            auth_token=settings.turso_auth_token,
        )
        conn.sync()
    else:
        conn = libsql.connect(settings.kairos_db_path)
    return conn


def _get_conn() -> Any:
    global _conn, _schema_ready
    if _conn is None:
        _conn = _connect()
        _schema_ready = False
    if not _schema_ready:
        _ensure_schema(_conn)
        _schema_ready = True
    return _conn


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kairos-db")
    return _executor


async def run(fn: Callable[[Any], T]) -> T:
    """Execute `fn(conn)` on the dedicated db thread. One call = one atomic unit."""

    def _task() -> T:
        conn = _get_conn()
        try:
            result = fn(conn)
            conn.commit()
            return result
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 — rollback outside txn is a no-op error
                pass
            raise

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_get_executor(), _task)


def set_db_persist(enabled: bool = True) -> None:
    """Keep the connection open across requests (FastAPI / long-running workers)."""
    global _persist_connection
    _persist_connection = enabled


async def close_db() -> None:
    """Close the shared connection. No-op when persist mode is on (web server)."""
    global _conn, _schema_ready
    if _persist_connection:
        return
    if _conn is not None:
        conn = _conn
        _conn = None
        _schema_ready = False
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_get_executor(), conn.close)


# ---------------------------------------------------------------------------
# Query helpers (call these from inside a `run()` closure or via the wrappers)
# ---------------------------------------------------------------------------


def rows_as_dicts(cursor: Any) -> list[dict[str, Any]]:
    cols = [d[0] for d in cursor.description or []]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


async def execute(sql: str, params: tuple = ()) -> None:
    await run(lambda conn: conn.execute(sql, params))


async def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return await run(lambda conn: rows_as_dicts(conn.execute(sql, params)))


async def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = await query(sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Vector support
# ---------------------------------------------------------------------------


def vector_supported(conn: Any) -> bool:
    """Detect native libSQL vector functions once per process."""
    global _vector_ok
    if _vector_ok is None:
        try:
            conn.execute("SELECT vector_distance_cos(vector32('[1,0]'), vector32('[0,1]'))")
            _vector_ok = True
        except Exception:  # noqa: BLE001 — older builds without vector functions
            _vector_ok = False
            logger.info("libSQL vector functions unavailable — using in-memory cosine fallback")
    return _vector_ok


def vector_param(embedding: list[float]) -> str:
    return json.dumps([float(x) for x in embedding])


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmarks (
    x_tweet_id      TEXT PRIMARY KEY,
    cluster_id      TEXT,
    ingested_at     TEXT,
    tweet_created_at TEXT,
    last_synced_at  TEXT,
    has_embedding   INTEGER NOT NULL DEFAULT 0,
    embedding       F32_BLOB({dims}),
    doc             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bookmarks_cluster ON bookmarks(cluster_id);
CREATE INDEX IF NOT EXISTS bookmarks_ingested ON bookmarks(ingested_at);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id          TEXT PRIMARY KEY,
    name                TEXT,
    member_count        INTEGER NOT NULL DEFAULT 0,
    centroid_embedding  F32_BLOB({dims}),
    doc                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bandit_params (
    user_id       TEXT NOT NULL,
    cluster_id    TEXT NOT NULL,
    context_class TEXT NOT NULL,
    alpha         REAL NOT NULL,
    beta          REAL NOT NULL,
    cohort_prior  INTEGER NOT NULL DEFAULT 0,
    last_updated  TEXT,
    PRIMARY KEY (user_id, cluster_id, context_class)
);
CREATE INDEX IF NOT EXISTS bandit_cluster_ctx ON bandit_params(cluster_id, context_class);

CREATE TABLE IF NOT EXISTS bandit_treatments (
    user_id       TEXT NOT NULL,
    cluster_id    TEXT NOT NULL,
    context_class TEXT NOT NULL,
    digest_style  TEXT NOT NULL,
    alpha         REAL NOT NULL,
    beta          REAL NOT NULL,
    last_updated  TEXT,
    PRIMARY KEY (user_id, cluster_id, context_class, digest_style)
);

CREATE TABLE IF NOT EXISTS feedback_events (
    event_id      TEXT PRIMARY KEY,
    notification_id TEXT,
    user_id       TEXT,
    cluster_id    TEXT,
    context_class TEXT,
    derived_reward REAL,
    has_snooze    INTEGER NOT NULL DEFAULT 0,
    sim           INTEGER NOT NULL DEFAULT 0,
    run_id        TEXT,
    persona       TEXT,
    created_at    TEXT NOT NULL,
    doc           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS feedback_user_ctx ON feedback_events(user_id, context_class, created_at);
CREATE INDEX IF NOT EXISTS feedback_cluster ON feedback_events(cluster_id, created_at);
CREATE INDEX IF NOT EXISTS feedback_created ON feedback_events(created_at);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    user_id       TEXT,
    status        TEXT,
    created_at    TEXT,
    doc           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, created_at);

CREATE TABLE IF NOT EXISTS optimization_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        TEXT NOT NULL,
    engagement_delta REAL,
    doc           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_cache (
    doc_id        TEXT PRIMARY KEY,
    user_id       TEXT,
    updated_at    TEXT,
    doc           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    source        TEXT PRIMARY KEY,
    doc           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS google_connections (
    user_id       TEXT PRIMARY KEY,
    email         TEXT,
    updated_at    TEXT,
    doc           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state         TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    message       TEXT NOT NULL,
    data          TEXT
);
CREATE INDEX IF NOT EXISTS pipeline_events_ts ON pipeline_events(timestamp);

CREATE TABLE IF NOT EXISTS prep_jobs (
    job_id        TEXT PRIMARY KEY,
    status        TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    doc           TEXT NOT NULL
);
"""


def _ensure_schema(conn: Any) -> None:
    ddl = _SCHEMA.format(dims=settings.gemini_embedding_dimensions)
    if not vector_supported(conn):
        # F32_BLOB degrades to BLOB affinity in plain SQLite; keep columns but
        # never write them — embeddings live in doc JSON either way.
        pass
    for statement in ddl.split(";"):
        statement = statement.strip()
        if statement:
            conn.execute(statement)
    conn.commit()
