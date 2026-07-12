"""Engagement metrics aggregated from feedback_events."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from kairos.db.bandit import bandit_user_id
from kairos.db.engine import iso, run


def _user_where(user_id: str | None, *, include_sim: bool = False) -> tuple[str, list[Any]]:
    uid = bandit_user_id(user_id)
    if include_sim and uid == "__default__":
        # Demo gym aggregate — personas use sim:* user_ids
        return "sim = 1", []
    return "user_id = ?", [uid]


async def get_engagement_by_day(
    days: int = 14,
    persona: str | None = None,
    include_sim: bool = True,
    *,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return daily engagement rates over the last `days` days.

    Each entry: {date: "YYYY-MM-DD", surfaces: int, engagements: int, rate: float}
    Ordered oldest → newest so the dashboard sparkline reads left-to-right.
    """
    since = iso(datetime.now(timezone.utc) - timedelta(days=days))
    where, params = _user_where(user_id, include_sim=include_sim)
    clauses = [f"created_at >= ?", where]
    values: list[Any] = [since, *params]
    if not include_sim:
        clauses.append("sim != 1")
    if persona:
        clauses.append("persona = ?")
        values.append(persona)

    def _task(conn: Any) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"""
            SELECT substr(created_at, 1, 10) AS date,
                   COUNT(*) AS surfaces,
                   SUM(CASE WHEN derived_reward > 0 THEN 1 ELSE 0 END) AS engagements
            FROM feedback_events
            WHERE {' AND '.join(clauses)}
            GROUP BY date
            ORDER BY date ASC
            """,
            tuple(values),
        ).fetchall()
        return [
            {
                "date": date,
                "surfaces": int(surfaces),
                "engagements": int(engagements or 0),
                "rate": (engagements or 0) / surfaces if surfaces else 0.0,
            }
            for date, surfaces, engagements in rows
        ]

    return await run(_task)


async def get_overall_stats(
    include_sim: bool = True,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Return aggregate counts: total surfaces, engagements, overall rate."""
    where, params = _user_where(user_id, include_sim=include_sim)
    clauses = [where]
    values: list[Any] = [*params]
    if not include_sim:
        clauses.append("sim != 1")

    def _task(conn: Any) -> dict[str, Any]:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total_surfaces,
                   SUM(CASE WHEN derived_reward > 0 THEN 1 ELSE 0 END) AS total_engagements
            FROM feedback_events
            WHERE {' AND '.join(clauses)}
            """,
            tuple(values),
        ).fetchone()
        surfaces = int(row[0] or 0)
        engagements = int(row[1] or 0)
        return {
            "total_surfaces": surfaces,
            "total_engagements": engagements,
            "overall_rate": engagements / surfaces if surfaces else 0.0,
        }

    return await run(_task)


def rate_change_pct(by_day: list[dict[str, Any]]) -> float | None:
    """Week-over-week change in engagement rate (percent points)."""
    if len(by_day) < 2:
        return None
    mid = len(by_day) // 2
    first = by_day[:mid]
    second = by_day[mid:]
    if not first or not second:
        return None

    def _avg_rate(rows: list[dict[str, Any]]) -> float:
        rates = [float(r.get("rate") or 0.0) for r in rows if r.get("surfaces")]
        if not rates:
            return 0.0
        return sum(rates) / len(rates)

    earlier = _avg_rate(first)
    recent = _avg_rate(second)
    if earlier == 0:
        return round(recent * 100, 1) if recent else None
    return round((recent - earlier) / earlier * 100, 1)
