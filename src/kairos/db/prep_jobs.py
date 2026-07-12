"""Background bookmark prep jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from kairos.db.engine import doc_dumps, doc_loads, iso, run
from kairos.models.jobs import PrepJobParams, PrepJobRecord, PrepJobResult, PrepJobStatus


def _parse_prep_job(doc: dict[str, Any]) -> PrepJobRecord:
    doc = dict(doc)
    params = doc.get("params") or {}
    result = doc.get("result")
    return PrepJobRecord(
        job_id=str(doc["job_id"]),
        status=doc.get("status", "pending"),
        params=PrepJobParams.model_validate(params),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
        result=PrepJobResult.model_validate(result) if result else None,
        error=doc.get("error"),
    )


async def ensure_prep_job_indexes() -> None:
    """Schema (tables + indexes) is created by the engine — nothing to do."""


async def create_prep_job(*, params: PrepJobParams | dict[str, Any] | None = None) -> PrepJobRecord:
    parsed = PrepJobParams.model_validate(params or {})
    job_id = str(uuid4())
    now = datetime.now(timezone.utc)
    doc = {
        "job_id": job_id,
        "status": "pending",
        "params": parsed.model_dump(mode="json"),
        "created_at": now,
        "updated_at": now,
        "result": None,
        "error": None,
    }
    await run(
        lambda conn: conn.execute(
            """
            INSERT INTO prep_jobs (job_id, status, created_at, updated_at, doc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, "pending", iso(now), iso(now), doc_dumps(doc)),
        )
    )
    return _parse_prep_job(doc)


async def update_prep_job(
    job_id: str,
    *,
    status: PrepJobStatus | None = None,
    result: PrepJobResult | dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    fields: dict[str, Any] = {"updated_at": now}
    if status is not None:
        fields["status"] = status
    if result is not None:
        if isinstance(result, PrepJobResult):
            fields["result"] = result.model_dump(mode="json")
        else:
            fields["result"] = result
    if error is not None:
        fields["error"] = error

    def _task(conn: Any) -> None:
        row = conn.execute(
            "SELECT doc FROM prep_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not row:
            return
        doc = doc_loads(row[0])
        doc.update(fields)
        conn.execute(
            "UPDATE prep_jobs SET status = ?, updated_at = ?, doc = ? WHERE job_id = ?",
            (doc.get("status"), iso(now), doc_dumps(doc), job_id),
        )

    await run(_task)


async def get_prep_job(job_id: str) -> PrepJobRecord | None:
    def _task(conn: Any) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT doc FROM prep_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return doc_loads(row[0]) if row else None

    doc = await run(_task)
    if not doc:
        return None
    return _parse_prep_job(doc)
