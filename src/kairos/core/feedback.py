"""Process user feedback — persist events and update bandit online."""

from __future__ import annotations

import logging

from kairos.core.moment import context_class
from kairos.core.rewards import reward_for_action
from kairos.db.bandit import apply_bandit_reward, apply_treatment_reward, ensure_bandit_indexes
from kairos.db.feedback import ensure_feedback_indexes, insert_feedback_event
from kairos.db.engine import close_db
from kairos.db.notifications import get_notification, update_notification_status
from kairos.delivery.render import digest_to_markdown
from kairos.models.schemas import FeedbackAction, NotificationStatus

logger = logging.getLogger(__name__)

_STATUS_FOR_ACTION: dict[FeedbackAction, NotificationStatus] = {
    "snoozed": "snoozed",
    "dismissed": "dismissed",
    "expanded": "acted",
    "link_click": "acted",
    "acted": "acted",
    "ignored": "expired",
}


async def process_feedback(
    notification_id: str,
    action: FeedbackAction,
    *,
    url: str | None = None,
    user_id: str | None = None,
) -> dict:
    """Write feedback_events and apply online bandit update when appropriate."""
    try:
        await ensure_feedback_indexes()
        await ensure_bandit_indexes()

        record = await get_notification(notification_id)
        if record is None:
            return {"status": "error", "message": f"unknown notification {notification_id}"}

        if user_id is not None and record.user_id:
            from kairos.db.bandit import bandit_user_id

            if record.user_id != bandit_user_id(user_id):
                return {"status": "error", "message": "notification not found for this user"}

        if not record.cluster_id or not record.context_snapshot:
            return {
                "status": "error",
                "message": "notification missing cluster_id or context_snapshot",
            }

        ctx_class = context_class(record.context_snapshot)
        reward = reward_for_action(action)
        markdown = digest_to_markdown(record.digest) if record.digest else ""

        event_id = await insert_feedback_event(
            notification_id=notification_id,
            cluster_id=record.cluster_id,
            context_class=ctx_class,
            context_snapshot=record.context_snapshot,
            action=action,
            derived_reward=reward,
            notification_text=markdown,
            url=url,
            user_id=record.user_id,
        )

        bandit_after: dict | None = None
        if reward is not None:
            bandit_after = await apply_bandit_reward(
                record.cluster_id,
                ctx_class,
                reward,
                user_id=record.user_id,
            )
            logger.info(
                "Bandit update cluster=%s context=%s reward=%s alpha=%s beta=%s",
                record.cluster_id[:8],
                ctx_class,
                reward,
                bandit_after["alpha"],
                bandit_after["beta"],
            )
            # GAMBITTS-lite: secondary update keyed on digest treatment style
            digest_style = (record.digest and record.digest.digest_style) or "standard"
            await apply_treatment_reward(
                record.cluster_id,
                ctx_class,
                digest_style,
                reward,
                user_id=record.user_id,
            )

        status = _STATUS_FOR_ACTION.get(action, "pending")
        await update_notification_status(notification_id, status)

        result: dict = {
            "status": "ok",
            "notification_id": notification_id,
            "action": action,
            "event_id": event_id,
            "derived_reward": reward,
            "cluster_id": record.cluster_id,
            "context_class": ctx_class,
        }
        if bandit_after:
            result["bandit"] = bandit_after
        if action == "snoozed":
            result["snooze_note"] = (
                f"Cluster snoozed for {ctx_class}; excluded from ranking until TTL expires."
            )
        return result
    finally:
        await close_db()
