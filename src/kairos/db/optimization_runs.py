"""Store GEPA optimization run results."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kairos.db.engine import doc_dumps, doc_loads, iso, run


async def save_optimization_run(
    prompt_before: str,
    prompt_after: str,
    engagement_before: float,
    engagement_after: float,
    diff_summary: str,
    sample_count: int = 0,
) -> str:
    """Persist a GEPA optimization run and return its id."""
    now = datetime.now(timezone.utc)
    doc: dict[str, Any] = {
        "run_at": now,
        "prompt_before": prompt_before,
        "prompt_after": prompt_after,
        "engagement_before": round(engagement_before, 4),
        "engagement_after": round(engagement_after, 4),
        "engagement_delta": round(engagement_after - engagement_before, 4),
        "diff_summary": diff_summary,
        "sample_count": sample_count,
    }

    def _task(conn: Any) -> str:
        cursor = conn.execute(
            "INSERT INTO optimization_runs (run_at, engagement_delta, doc) VALUES (?, ?, ?)",
            (iso(now), doc["engagement_delta"], doc_dumps(doc)),
        )
        return str(cursor.lastrowid)

    return await run(_task)


async def get_active_prompt() -> str | None:
    """Return the most recent optimized digest prompt, or None to use the default."""

    def _task(conn: Any) -> str | None:
        row = conn.execute(
            """
            SELECT doc FROM optimization_runs
            WHERE engagement_delta > 0
            ORDER BY run_at DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        return doc_loads(row[0]).get("prompt_after")

    return await run(_task)


async def list_optimization_runs(limit: int = 10) -> list[dict[str, Any]]:
    def _task(conn: Any) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT id, doc FROM optimization_runs ORDER BY run_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row_id, doc_text in rows:
            doc = doc_loads(doc_text)
            doc["id"] = str(row_id)
            run_at = doc.get("run_at")
            if run_at is not None and hasattr(run_at, "isoformat"):
                doc["run_at"] = run_at.isoformat()
            out.append(doc)
        return out

    return await run(_task)
