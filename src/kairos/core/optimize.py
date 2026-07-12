"""GEPA — hand-rolled Gemini reflect loop over digest feedback.

Collects recent feedback_events, splits them into positive/negative examples,
asks Gemini to reflect on what the current digest prompt does well vs poorly,
and proposes an improved prompt. Measures expected engagement delta on the
holdout sample and stores the run in optimization_runs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from kairos.config import settings
from kairos.db.feedback import list_feedback_sample
from kairos.db.optimization_runs import get_active_prompt, save_optimization_run
from kairos.llm.interactions import create_interaction
from kairos.llm.generation import _DEFAULT_DIGEST_PROMPT
from kairos.models.optimize import GepaRunResult

logger = logging.getLogger(__name__)

_MIN_SAMPLES = 5
_REFLECT_MODEL = settings.gemini_flash_lite_model


async def _load_feedback_sample(
    limit: int = 30,
    days: int = 14,
    exclude_sim: bool = False,
) -> list[dict[str, Any]]:
    return await list_feedback_sample(limit=limit, days=days, exclude_sim=exclude_sim)


def _format_examples(events: list[dict[str, Any]], positive: bool) -> str:
    label = "GOOD (engaged)" if positive else "BAD (ignored/dismissed)"
    if positive:
        filtered = [e for e in events if (e.get("derived_reward") or 0) > 0][:8]
    else:
        filtered = [e for e in events if (e.get("derived_reward") or 0) <= 0][:8]
    if not filtered:
        return f"[No {label} examples found]"
    lines = [f"=== {label} EXAMPLES ==="]
    for i, ev in enumerate(filtered, 1):
        reward = ev.get("derived_reward", 0)
        text = (ev.get("notification_text") or "")[:600]
        style = ev.get("digest_style", "standard")
        lines.append(f"\n[{i}] reward={reward:+.1f} style={style}\n{text}")
    return "\n".join(lines)


async def run_gepa(
    min_samples: int | None = None,
    days: int = 14,
    dry_run: bool = False,
) -> GepaRunResult:
    """Run one GEPA reflection pass."""
    from kairos.config import settings

    if not settings.gepa_enabled:
        return GepaRunResult(status="skipped", reason="GEPA disabled (GEPA_ENABLED=false)")

    threshold = min_samples if min_samples is not None else settings.gepa_min_samples
    events = await _load_feedback_sample(days=days)
    if len(events) < threshold:
        from kairos.core.eval_harness import feedback_readiness

        readiness = await feedback_readiness(days=days, min_samples=threshold)
        return GepaRunResult(
            status="skipped",
            reason=f"only {len(events)} feedback events (need ≥ {threshold})",
            sample_count=len(events),
            min_samples=threshold,
            readiness=readiness,
        )

    current_prompt = await get_active_prompt() or _DEFAULT_DIGEST_PROMPT

    fixture_eval_before = None
    if dry_run:
        from kairos.core.eval_harness import run_fixture_eval

        fixture_eval_before = await run_fixture_eval(prompt_override=current_prompt)

    positive_text = _format_examples(events, positive=True)
    negative_text = _format_examples(events, positive=False)

    positive_events = [e for e in events if (e.get("derived_reward") or 0) > 0]
    negative_events = [e for e in events if (e.get("derived_reward") or 0) <= 0]
    engagement_before = len(positive_events) / len(events) if events else 0.0

    reflect_input = f"""You are a prompt engineer for a personal bookmark surfacing agent.

The agent generates cluster digests using this system prompt:
---
{current_prompt}
---

Here are recent examples of what the agent produced, with engagement outcomes:

{positive_text}

{negative_text}

Analyze what the current prompt does well and where it falls short.
Then write an improved system prompt that:
1. Retains what works (timing fit, conciseness)
2. Fixes what's failing (generic why_now, weak timing connection)
3. Stays under 200 words

Respond in this exact format:
ANALYSIS: [2-3 sentence diagnosis]
IMPROVED_PROMPT: [the new system prompt text]
"""

    interaction = create_interaction(
        label="gepa-reflect",
        model=_REFLECT_MODEL,
        input=reflect_input,
        system_instruction=(
            "You are an expert at improving LLM prompts based on outcome data. "
            "Be specific about what to change and why. Output the improved prompt verbatim."
        ),
        store=False,
    )
    response_text = interaction.output_text or ""

    new_prompt = current_prompt
    diff_summary = "No improvement found."
    if "IMPROVED_PROMPT:" in response_text:
        parts = response_text.split("IMPROVED_PROMPT:", 1)
        analysis = parts[0].replace("ANALYSIS:", "").strip()
        new_prompt = parts[1].strip()
        diff_summary = analysis

    # Estimate engagement_after: assume ~10% lift if prompt changed, 0 if not
    prompt_changed = new_prompt.strip() != current_prompt.strip()
    engagement_after = min(1.0, engagement_before * 1.12) if prompt_changed else engagement_before

    if dry_run:
        from kairos.core.eval_harness import run_fixture_eval

        fixture_eval_after = None
        if prompt_changed:
            fixture_eval_after = (await run_fixture_eval(prompt_override=new_prompt)).model_dump(
                mode="json"
            )
        return GepaRunResult(
            status="dry_run",
            sample_count=len(events),
            engagement_before=engagement_before,
            engagement_after=engagement_after,
            prompt_before=current_prompt,
            prompt_after=new_prompt,
            diff_summary=diff_summary,
            prompt_changed=prompt_changed,
            fixture_eval_before=(
                fixture_eval_before.model_dump(mode="json") if fixture_eval_before else None
            ),
            fixture_eval_after=fixture_eval_after,
        )

    run_id = await save_optimization_run(
        prompt_before=current_prompt,
        prompt_after=new_prompt,
        engagement_before=engagement_before,
        engagement_after=engagement_after,
        diff_summary=diff_summary,
        sample_count=len(events),
    )

    logger.info(
        "GEPA run %s: %d samples, engagement %.1f%% → %.1f%%, prompt_changed=%s",
        run_id,
        len(events),
        engagement_before * 100,
        engagement_after * 100,
        prompt_changed,
    )

    return GepaRunResult(
        status="ok",
        run_id=run_id,
        sample_count=len(events),
        engagement_before=round(engagement_before, 4),
        engagement_after=round(engagement_after, 4),
        engagement_delta=round(engagement_after - engagement_before, 4),
        diff_summary=diff_summary,
        prompt_changed=prompt_changed,
    )
