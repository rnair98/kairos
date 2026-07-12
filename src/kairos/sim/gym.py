"""Persona gym — run the real bandit loop with synthetic users."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from kairos.core.bandit import thompson_sample
from kairos.core.moment import context_class
from kairos.core.ranking import evaluate_surface
from kairos.core.rewards import reward_for_action
from kairos.db.bandit import apply_bandit_reward, ensure_bandit_indexes, list_bandit_params
from kairos.db.clusters import list_clusters
from kairos.db.feedback import (
    delete_sim_feedback,
    ensure_feedback_indexes,
    insert_sim_feedback_event,
)
from kairos.db.bandit import reset_bandit_params
from kairos.db.engine import close_db
from kairos.models.schemas import FeedbackAction
from kairos.sim.context_sampler import TICKS_PER_DAY, _tick_hour, _daily_busy_ticks, sample_context
from kairos.sim.feedback_model import simulate_feedback
from kairos.sim.persona import ALL_PERSONAS, Persona

logger = logging.getLogger(__name__)


@dataclass
class DayResult:
    surfaces: int = 0
    engagements: int = 0

    @property
    def rate(self) -> float:
        return self.engagements / self.surfaces if self.surfaces else 0.0


@dataclass
class GymResult:
    run_id: str
    personas: list[str]
    days: int
    total_ticks: int = 0
    total_surfaces: int = 0
    total_engagements: int = 0
    # persona_name → list of daily engagement rates
    engagement_by_day: dict[str, list[float]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def overall_rate(self) -> float:
        return self.total_engagements / self.total_surfaces if self.total_surfaces else 0.0


async def _insert_sim_feedback(
    *,
    run_id: str,
    persona_name: str,
    cluster_id: str,
    ctx_class: str,
    context_snapshot: dict,
    action: FeedbackAction,
    derived_reward: float | None,
) -> None:
    """Write a sim-tagged feedback event without needing a real notification lookup."""
    doc = {
        "event_id": str(uuid4()),
        "notification_id": f"sim_{uuid4().hex[:12]}",
        "user_id": f"sim:{persona_name}",
        "cluster_id": cluster_id,
        "context_class": ctx_class,
        "context_snapshot": context_snapshot,
        "notification_text": "",
        "events": [{"type": "shown", "t": 0}, {"type": action, "t": 0}],
        "derived_reward": derived_reward,
        "snooze_context": context_snapshot if action == "snoozed" else None,
        "sim": True,
        "run_id": run_id,
        "persona": persona_name,
        "created_at": datetime.now(timezone.utc),
    }
    await insert_sim_feedback_event(doc)


async def run_gym(
    personas: list[Persona] | None = None,
    days: int = 14,
    ticks_per_day: int = TICKS_PER_DAY,
    run_id: str | None = None,
) -> GymResult:
    """
    Run the real bandit + ranking loop against synthetic personas.

    Uses evaluate_surface with generate_digest=False so the gym completes
    in seconds rather than minutes (personas react to cluster topic, not prose).
    Feedback events are tagged sim=True + run_id for selective reset.
    """
    if personas is None:
        personas = list(ALL_PERSONAS.values())

    run_id = run_id or str(uuid4())[:8]
    result = GymResult(
        run_id=run_id,
        personas=[p.name for p in personas],
        days=days,
    )

    await ensure_bandit_indexes()
    await ensure_feedback_indexes()
    clusters = await list_clusters()

    cluster_name_map: dict[str, str] = {
        c["cluster_id"]: c.get("name") or c["cluster_id"]
        for c in clusters
    }

    logger.info(
        "Gym starting: run_id=%s personas=%s days=%d ticks=%d clusters=%d",
        run_id, [p.name for p in personas], days, ticks_per_day, len(clusters),
    )

    for persona in personas:
        day_rates: list[float] = []

        for day in range(days):
            day_result = DayResult()
            busy_ticks = _daily_busy_ticks(persona, day)
            surfaces_today = 0
            time_since_last_surface = 9999

            for tick in range(ticks_per_day):
                result.total_ticks += 1
                ctx = sample_context(
                    persona, day, tick,
                    surfaces_today=surfaces_today,
                    time_since_last_surface_minutes=time_since_last_surface,
                    busy_ticks=busy_ticks,
                )

                try:
                    decision = await evaluate_surface(
                        ctx,
                        user_id=f"sim:{persona.name}",
                        generate_digest=False,
                        _keep_db_open=True,
                    )
                except Exception as exc:
                    result.errors.append(f"{persona.name} day={day} tick={tick}: {exc}")
                    continue

                time_since_last_surface = min(time_since_last_surface + 30, 9999)

                if not decision.should_surface or not decision.cluster_id:
                    continue

                surfaces_today += 1
                time_since_last_surface = 0
                day_result.surfaces += 1
                result.total_surfaces += 1

                cluster_name = cluster_name_map.get(decision.cluster_id, "")
                hour = _tick_hour(tick)
                action = simulate_feedback(
                    persona, cluster_name, ctx, hour,
                    seed=hash((run_id, persona.name, day, tick)) & 0x7FFFFFFF,
                )

                reward = reward_for_action(action)
                ctx_class = context_class(ctx)

                await _insert_sim_feedback(
                    run_id=run_id,
                    persona_name=persona.name,
                    cluster_id=decision.cluster_id,
                    ctx_class=ctx_class,
                    context_snapshot=ctx.model_dump(),
                    action=action,
                    derived_reward=reward,
                )

                if reward is not None:
                    await apply_bandit_reward(
                        decision.cluster_id,
                        ctx_class,
                        reward,
                        user_id=f"sim:{persona.name}",
                    )

                is_engaged = action in ("link_click", "expanded", "acted")
                if is_engaged:
                    day_result.engagements += 1
                    result.total_engagements += 1

            day_rates.append(day_result.rate)
            logger.debug(
                "Gym %s day=%d surfaces=%d engaged=%d rate=%.2f",
                persona.name, day, day_result.surfaces,
                day_result.engagements, day_result.rate,
            )

        result.engagement_by_day[persona.name] = day_rates
        logger.info(
            "Gym %s done: avg_rate=%.2f",
            persona.name,
            sum(day_rates) / len(day_rates) if day_rates else 0,
        )

    await close_db()

    logger.info(
        "Gym complete: run_id=%s surfaces=%d engagements=%d rate=%.2f errors=%d",
        run_id, result.total_surfaces, result.total_engagements,
        result.overall_rate, len(result.errors),
    )
    return result


async def reset_gym(run_id: str | None = None) -> dict:
    """
    Delete sim-tagged feedback events and reset all bandit params to defaults.

    If run_id is given, only deletes events from that run.
    Bandit params are always fully reset (they don't carry run_id).
    """
    deleted_feedback = await delete_sim_feedback(run_id)
    deleted_bandit = await reset_bandit_params()

    await close_db()

    return {
        "deleted_feedback_events": deleted_feedback,
        "reset_bandit_params": deleted_bandit,
        "run_id": run_id or "all",
    }
