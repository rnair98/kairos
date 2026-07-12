"""Bandit parameter store — Thompson sampling α/β per user × cluster × context."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from kairos.config import settings
from kairos.db.engine import iso, rows_as_dicts, run

DEFAULT_ALPHA = 1.0
DEFAULT_BETA = 1.0


def _cohort_prior_sync(
    conn: Any,
    cluster_id: str,
    context_class: str,
    *,
    exclude_user_id: str,
) -> tuple[float, float] | None:
    """Mean α/β from other users on the same cluster×context (cold-start prior)."""
    if not settings.cohort_prior_enabled:
        return None
    row = conn.execute(
        """
        SELECT AVG(alpha) AS alpha, AVG(beta) AS beta, COUNT(DISTINCT user_id) AS users
        FROM bandit_params
        WHERE cluster_id = ? AND context_class = ? AND user_id != ?
        """,
        (cluster_id, context_class, exclude_user_id),
    ).fetchone()
    if not row or row[0] is None or (row[2] or 0) < settings.cohort_prior_min_users:
        return None
    return float(row[0]), float(row[1])


def _apply_prior(params: dict[str, Any], prior: tuple[float, float] | None) -> dict[str, Any]:
    if prior is None:
        return params
    alpha, beta = prior
    if float(params.get("alpha", DEFAULT_ALPHA)) != DEFAULT_ALPHA or float(
        params.get("beta", DEFAULT_BETA)
    ) != DEFAULT_BETA:
        return params
    merged = dict(params)
    merged["alpha"] = alpha
    merged["beta"] = beta
    merged["cohort_prior"] = True
    return merged


def bandit_user_id(user_id: str | None = None) -> str:
    return user_id or settings.kairos_user_id or "__default__"


async def ensure_bandit_indexes() -> None:
    """Schema (tables + indexes) is created by the engine — nothing to do."""


def _defaults(uid: str, cluster_id: str, context_class: str) -> dict[str, Any]:
    return {
        "user_id": uid,
        "cluster_id": cluster_id,
        "context_class": context_class,
        "alpha": DEFAULT_ALPHA,
        "beta": DEFAULT_BETA,
    }


def _fetch_params_sync(
    conn: Any, uid: str, cluster_id: str, context_class: str
) -> dict[str, Any]:
    rows = rows_as_dicts(
        conn.execute(
            """
            SELECT user_id, cluster_id, context_class, alpha, beta, cohort_prior, last_updated
            FROM bandit_params
            WHERE user_id = ? AND cluster_id = ? AND context_class = ?
            """,
            (uid, cluster_id, context_class),
        )
    )
    if rows:
        return rows[0]
    prior = _cohort_prior_sync(conn, cluster_id, context_class, exclude_user_id=uid)
    return _apply_prior(_defaults(uid, cluster_id, context_class), prior)


async def get_bandit_params(
    cluster_id: str,
    context_class: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Return α/β for a user×cluster×context pair, creating defaults if missing."""
    uid = bandit_user_id(user_id)
    return await run(lambda conn: _fetch_params_sync(conn, uid, cluster_id, context_class))


async def get_bandit_params_batch(
    cluster_ids: list[str],
    context_class: str,
    *,
    user_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch bandit params for many clusters in one query."""
    if not cluster_ids:
        return {}
    uid = bandit_user_id(user_id)

    def _task(conn: Any) -> dict[str, dict[str, Any]]:
        placeholders = ",".join("?" for _ in cluster_ids)
        rows = rows_as_dicts(
            conn.execute(
                f"""
                SELECT user_id, cluster_id, context_class, alpha, beta, cohort_prior, last_updated
                FROM bandit_params
                WHERE user_id = ? AND context_class = ? AND cluster_id IN ({placeholders})
                """,
                (uid, context_class, *cluster_ids),
            )
        )
        by_cluster = {row["cluster_id"]: row for row in rows}
        for cluster_id in cluster_ids:
            if cluster_id in by_cluster:
                continue
            prior = _cohort_prior_sync(conn, cluster_id, context_class, exclude_user_id=uid)
            by_cluster[cluster_id] = _apply_prior(
                _defaults(uid, cluster_id, context_class), prior
            )
        return by_cluster

    return await run(_task)


async def apply_bandit_reward(
    cluster_id: str,
    context_class: str,
    reward: float,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Online update — increment α on positive reward, β on negative."""
    now = datetime.now(timezone.utc)
    uid = bandit_user_id(user_id)
    alpha_delta, beta_delta = (reward, 0.0) if reward > 0 else (0.0, abs(reward))

    def _task(conn: Any) -> dict[str, Any]:
        params = _fetch_params_sync(conn, uid, cluster_id, context_class)
        new_alpha = float(params.get("alpha", DEFAULT_ALPHA)) + alpha_delta
        new_beta = float(params.get("beta", DEFAULT_BETA)) + beta_delta
        conn.execute(
            """
            INSERT INTO bandit_params (user_id, cluster_id, context_class, alpha, beta, cohort_prior, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, cluster_id, context_class)
            DO UPDATE SET alpha = excluded.alpha, beta = excluded.beta,
                          last_updated = excluded.last_updated
            """,
            (
                uid,
                cluster_id,
                context_class,
                new_alpha,
                new_beta,
                1 if params.get("cohort_prior") else 0,
                iso(now),
            ),
        )
        return {
            "user_id": uid,
            "cluster_id": cluster_id,
            "context_class": context_class,
            "alpha": new_alpha,
            "beta": new_beta,
        }

    return await run(_task)


async def get_treatment_params(
    cluster_id: str,
    context_class: str,
    digest_style: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Return α/β for a user×cluster×context×treatment tuple (GAMBITTS-lite)."""
    uid = bandit_user_id(user_id)

    def _task(conn: Any) -> dict[str, Any]:
        rows = rows_as_dicts(
            conn.execute(
                """
                SELECT user_id, cluster_id, context_class, digest_style, alpha, beta, last_updated
                FROM bandit_treatments
                WHERE user_id = ? AND cluster_id = ? AND context_class = ? AND digest_style = ?
                """,
                (uid, cluster_id, context_class, digest_style),
            )
        )
        if rows:
            return rows[0]
        return {
            "user_id": uid,
            "cluster_id": cluster_id,
            "context_class": context_class,
            "digest_style": digest_style,
            "alpha": DEFAULT_ALPHA,
            "beta": DEFAULT_BETA,
        }

    return await run(_task)


async def apply_treatment_reward(
    cluster_id: str,
    context_class: str,
    digest_style: str,
    reward: float,
    *,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Secondary bandit update keyed on treatment (GAMBITTS-lite)."""
    now = datetime.now(timezone.utc)
    uid = bandit_user_id(user_id)
    alpha_delta, beta_delta = (reward, 0.0) if reward > 0 else (0.0, abs(reward))

    def _task(conn: Any) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT alpha, beta FROM bandit_treatments
            WHERE user_id = ? AND cluster_id = ? AND context_class = ? AND digest_style = ?
            """,
            (uid, cluster_id, context_class, digest_style),
        ).fetchone()
        alpha = float(row[0]) if row else DEFAULT_ALPHA
        beta = float(row[1]) if row else DEFAULT_BETA
        new_alpha = alpha + alpha_delta
        new_beta = beta + beta_delta
        conn.execute(
            """
            INSERT INTO bandit_treatments (user_id, cluster_id, context_class, digest_style, alpha, beta, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, cluster_id, context_class, digest_style)
            DO UPDATE SET alpha = excluded.alpha, beta = excluded.beta,
                          last_updated = excluded.last_updated
            """,
            (uid, cluster_id, context_class, digest_style, new_alpha, new_beta, iso(now)),
        )
        return {
            "user_id": uid,
            "cluster_id": cluster_id,
            "context_class": context_class,
            "digest_style": digest_style,
            "alpha": new_alpha,
            "beta": new_beta,
        }

    return await run(_task)


async def list_bandit_params(*, limit: int = 20, user_id: str | None = None) -> list[dict[str, Any]]:
    uid = bandit_user_id(user_id)
    return await run(
        lambda conn: rows_as_dicts(
            conn.execute(
                """
                SELECT user_id, cluster_id, context_class, alpha, beta, cohort_prior, last_updated
                FROM bandit_params
                WHERE user_id = ?
                ORDER BY last_updated DESC
                LIMIT ?
                """,
                (uid, limit),
            )
        )
    )


async def reset_bandit_params() -> int:
    """Delete all bandit posteriors (gym reset). Returns rows removed."""

    def _task(conn: Any) -> int:
        n = conn.execute("SELECT COUNT(*) FROM bandit_params").fetchone()[0]
        conn.execute("DELETE FROM bandit_params")
        conn.execute("DELETE FROM bandit_treatments")
        return int(n)

    return await run(_task)
