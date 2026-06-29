"""Render digests for host agents and delivery adapters."""

from __future__ import annotations

from kairos.config import settings
from kairos.models.schemas import ClusterDigest, DeliveryHints, HeartbeatResult


def digest_to_markdown(digest: ClusterDigest) -> str:
    lines = [
        f"## {digest.cluster_name} ({digest.member_count} bookmarks)",
        "",
        digest.summary,
        "",
        f"**Why now:** {digest.why_now}",
        "",
    ]
    if digest.web_context:
        lines.extend(["**From the web:**", "", digest.web_context, ""])
    _STATUS_MARK = {"current": "✓ current", "dated": "● dated", "stale": "✗ stale"}
    for link in digest.links:
        title = link.title or link.label or link.url or "link"
        mode = link.consumption_mode or ""
        suffix = f" — {mode}" if mode else ""
        lines.append(f"### {title}{suffix}")
        if link.author:
            lines.append(f"*{link.author}*")
        body = link.summary or link.excerpt
        if body:
            lines.append("")
            lines.append(body)
        if link.excerpt and link.summary and link.excerpt != link.summary:
            lines.append(f"> Saved: {link.excerpt}")
        signal = link.signal
        status_mark = _STATUS_MARK.get(link.status or "")
        if status_mark:
            lines.append(f"*{status_mark}*")
        if signal:
            lines.append(f"_{signal}_")
        if link.tags:
            lines.append(f"Tags: {', '.join(link.tags)}")
        lines.append(f"[Open link]({link.url or '#'})")
        lines.append("")
    if digest.citations:
        lines.extend(["", "**Sources:**"])
        for citation in digest.citations[:8]:
            title = citation.title or citation.url
            lines.append(f"- [{title}]({citation.url})")
    return "\n".join(lines)


def build_delivery_hints(result: HeartbeatResult) -> DeliveryHints:
    """Build MCP/host-facing presentation hints for a SURFACE heartbeat."""
    digest = result.notification.digest if result.notification else None
    markdown = digest_to_markdown(digest) if digest else ""
    notification_id = result.notification.notification_id if result.notification else None
    dashboard_url = (
        f"{settings.web_base_url.rstrip('/')}/n/{notification_id}"
        if notification_id
        else None
    )

    actions: list[str] = []
    if digest:
        actions.append("Show the rendered_markdown digest to the user in chat.")
        if dashboard_url:
            actions.append(
                f"If the Kairos dashboard is running, direct the user to {dashboard_url}."
            )
            actions.append(
                "If the dashboard is not running, offer to start it with `kairos serve`."
            )
        actions.append(
            "Ask the user for feedback (relevant / snooze / dismiss) and call "
            "record_feedback with their response."
        )
        if settings.os_delivery_enabled:
            actions.append(
                "On macOS with os delivery enabled, optionally run terminal-notifier "
                "with a short summary if the user wants OS alerts."
            )

    return DeliveryHints(
        rendered_markdown=markdown,
        dashboard_url=dashboard_url,
        suggested_host_actions=actions,
        suppress_ok_in_chat=settings.mcp_suppress_ok_in_chat,
    )


def ok_reason(decision_gate_reasons: dict[str, bool]) -> str:
    failed = [k for k, passed in decision_gate_reasons.items() if not passed]
    if not failed:
        return "interrupt gate closed"
    return f"gate failed: {', '.join(failed)}"
