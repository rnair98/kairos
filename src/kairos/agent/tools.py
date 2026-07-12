"""Kairos tools — thin wrappers over policy core (usable by ADK agent + MCP)."""

from __future__ import annotations

import asyncio
from typing import Any

from kairos.config import settings
from kairos.core.context import get_context_async, read_context, write_context
from kairos.core.headspace import enrich_modes
from kairos.core.heartbeat import heartbeat_service
from kairos.db.clusters import list_clusters
from kairos.db.engine import close_db
from kairos.embeddings.encoder import encode_query
from kairos.models.schemas import ContextSnapshot, DeliveryMode, FeedbackAction, LocationType
from kairos.observability.bus import event_bus


def get_current_context(user_id: str | None = None) -> dict[str, Any]:
    """Read the live fused headspace vector for a user."""
    uid = user_id or settings.kairos_user_id
    return read_context(uid).model_dump(mode="json")


def connect_google(
    open_browser: bool = False,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Connect Google Calendar + Gmail via loopback OAuth callback.

    Starts a local listener on GOOGLE_OAUTH_REDIRECT_URI (default :8766/callback),
    returns when the user completes consent in the browser.

    Args:
        open_browser: Open the authorization URL automatically.
        timeout_seconds: Max seconds to wait for callback.
    """
    from kairos.google.connect import connect_google as _connect

    return asyncio.run(
        _connect(open_browser=open_browser, timeout_seconds=float(timeout_seconds))
    )


def start_google_connect(open_browser: bool = False) -> dict[str, Any]:
    """Begin Google OAuth — returns authorization_url for user consent.

    After the user approves, call wait_google_connect(state=...).
    """
    from kairos.google.connect import start_google_connect as _start

    return asyncio.run(_start(open_browser=open_browser))


def wait_google_connect(state: str, timeout_seconds: int = 300) -> dict[str, Any]:
    """Wait for Google OAuth callback after start_google_connect.

    Args:
        state: OAuth state from start_google_connect.
        timeout_seconds: Max seconds to wait.
    """
    from kairos.google.connect import wait_google_connect as _wait

    return _wait(state, timeout_seconds=float(timeout_seconds))


def google_connect_status(state: str | None = None) -> dict[str, Any]:
    """Poll in-flight OAuth session status (non-blocking).

    Args:
        state: OAuth state from start_google_connect, or None to list pending.
    """
    from kairos.google.connect import google_connect_status as _status

    return _status(state)


def sync_google_headspace(
    user_id: str | None = None,
    location_type: LocationType | None = None,
    lat: float | None = None,
    lng: float | None = None,
) -> dict[str, Any]:
    """Fetch Calendar + Gmail for a connected user and fuse headspace.

    Requires connect_google first. Pass user_id or set KAIROS_USER_ID in MCP env.

    Args:
        user_id: Google account id (sub) from OAuth callback.
        location_type: Optional location override after sync.
        lat: Device latitude (optional).
        lng: Device longitude (optional).
    """
    uid = user_id or settings.kairos_user_id
    if not uid:
        return {
            "ok": False,
            "issues": ["user_id required — run connect_google first or set KAIROS_USER_ID"],
        }

    async def _run() -> dict[str, Any]:
        from kairos.google.headspace_sync import sync_google_headspace as _sync

        result = await _sync(
            uid,
            persist=True,
            location_type=location_type,
            lat=lat,
            lng=lng,
        )
        return result

    return asyncio.run(_run())


def get_relevant_bookmarks(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Semantic search over the bookmark index.

    Args:
        query: Natural language search query.
        limit: Maximum bookmarks to return.
    """
    async def _run() -> list[dict[str, Any]]:
        from kairos.bookmarks.search import search_bookmarks

        try:
            return await search_bookmarks(query, limit=limit)
        finally:
            await close_db()

    results = asyncio.run(_run())
    event_bus.emit(
        "search",
        f"Semantic search: {query!r}",
        query=query,
        limit=limit,
        results=len(results),
    )
    return results


async def _cluster_for_topic(topic: str) -> dict[str, Any] | None:
    from kairos.embeddings.similarity import cosine_similarity

    clusters = await list_clusters()
    clusters = [c for c in clusters if c.get("centroid_embedding")]
    if not clusters:
        return None
    vector = encode_query(topic)
    best: dict[str, Any] | None = None
    best_score = -1.0
    for cluster in clusters:
        score = cosine_similarity(vector, cluster["centroid_embedding"])
        if score > best_score:
            best_score = score
            best = cluster
    return best


def get_cluster_summary(topic: str) -> dict[str, Any] | None:
    """Return the cluster closest to topic with its generated summary.

    Args:
        topic: Topic label or natural language description.
    """
    async def _run() -> dict[str, Any] | None:
        try:
            cluster = await _cluster_for_topic(topic)
            if not cluster:
                return None
            return {
                "cluster_id": cluster.get("cluster_id"),
                "name": cluster.get("name"),
                "summary": cluster.get("summary"),
                "member_count": cluster.get("member_count"),
            }
        finally:
            await close_db()

    result = asyncio.run(_run())
    event_bus.emit("cluster", f"Lookup cluster for {topic!r}", topic=topic, found=bool(result))
    return result


def run_heartbeat(
    delivery: DeliveryMode = "auto",
    context_override: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Run one heartbeat: context → rank → gate → publish to configured targets.

    Returns KAIROS_OK when silent or SURFACE with digest + host delivery hints.
    MCP clients should render delivery.rendered_markdown in chat on SURFACE.

    Args:
        delivery: auto (configured adapters), return_only (no side effects), none.
        context_override: Optional free-text context hint for demo overrides.
        user_id: Google sub or KAIROS_USER_ID; defaults to settings.kairos_user_id.
    """
    uid = user_id or settings.kairos_user_id
    result = asyncio.run(
        heartbeat_service.run(
            delivery=delivery,
            context_override=context_override,
            user_id=uid,
        )
    )
    return result.model_dump()


async def run_heartbeat_async(
    delivery: DeliveryMode = "auto",
    context_override: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Async heartbeat for harness and HTTP handlers."""
    uid = user_id or settings.kairos_user_id
    result = await heartbeat_service.run(
        delivery=delivery,
        context_override=context_override,
        user_id=uid,
    )
    return result.model_dump()


def record_feedback(
    notification_id: str,
    action: FeedbackAction,
    url: str | None = None,
) -> dict[str, str]:
    """Record user feedback on a surfaced digest (any host: web, MCP chat, etc.)."""
    return asyncio.run(
        heartbeat_service.record_feedback(notification_id, action, url=url)
    )


async def record_feedback_async(
    notification_id: str,
    action: FeedbackAction,
    url: str | None = None,
) -> dict:
    return await heartbeat_service.record_feedback(notification_id, action, url=url)


def add_bookmark(url: str, notes: str = "") -> dict[str, str]:
    """Manual bookmark ingest is not supported — use X sync (`kairos x sync`)."""
    event_bus.emit("ingest", f"Ingest rejected for {url}", url=url, notes=notes)
    return {
        "status": "unsupported",
        "message": "Use kairos x sync to ingest bookmarks from X.",
        "url": url,
    }


def fuse_headspace_context(
    calendar_events: list[dict[str, Any]] | None = None,
    email_threads: list[dict[str, Any]] | None = None,
    email_themes: list[str] | None = None,
    location_type: LocationType | None = None,
    lat: float | None = None,
    lng: float | None = None,
    surfaces_today: int | None = None,
    time_since_last_surface_minutes: int | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Fuse Calendar/Gmail/geo sensor readings into headspace and persist.

    Prefer sync_google_headspace after connect_google.
    Use this when you already have raw event/thread payloads (manual or test fixtures).

    Args:
        calendar_events: Raw event dicts from Google Calendar API shape.
        email_threads: Raw thread dicts from Gmail (subject/snippet used).
        email_themes: Pre-extracted email topic strings (optional if threads provided).
        location_type: desk | cafe | commute | gym | near_anchor | unknown.
        lat: Device latitude (optional).
        lng: Device longitude (optional).
        surfaces_today: Surfaces already shown today (fatigue proxy).
        time_since_last_surface_minutes: Minutes since last surface.
        user_id: Persist context for this user (defaults to KAIROS_USER_ID).
    """
    async def _run() -> ContextSnapshot:
        from kairos.google.headspace_sync import fuse_and_persist_headspace

        uid = user_id or settings.kairos_user_id
        return await fuse_and_persist_headspace(
            user_id=uid,
            calendar_events=calendar_events,
            email_threads=email_threads,
            email_themes=email_themes,
            location_type=location_type,
            lat=lat,
            lng=lng,
            surfaces_today=surfaces_today,
            time_since_last_surface_minutes=time_since_last_surface_minutes,
            sensor_sources=["mcp_fuse"],
        )

    result = asyncio.run(_run())
    return {"status": "ok", "context": result.model_dump(mode="json")}


def set_context(
    upcoming_event_title: str | None = None,
    recent_event_title: str | None = None,
    post_meeting_minutes: int | None = None,
    location_type: LocationType | None = None,
    calendar_gap_minutes: int | None = None,
    meeting_density_today: float | None = None,
    minutes_since_last_meeting: int | None = None,
    surfaces_today: int | None = None,
    time_since_last_surface_minutes: int | None = None,
    email_themes: list[str] | None = None,
    communication_burst: bool | None = None,
    lat: float | None = None,
    lng: float | None = None,
) -> dict[str, Any]:
    """Set or patch the fused headspace snapshot used by run_heartbeat.

    Use fuse_headspace_context when raw Calendar/Gmail payloads are available.
    Use this for direct field updates or demo overrides.
    """
    async def _run() -> ContextSnapshot:
        base = await get_context_async(settings.kairos_user_id)
        updates = {
            k: v
            for k, v in {
                "upcoming_event_title": upcoming_event_title,
                "recent_event_title": recent_event_title,
                "post_meeting_minutes": post_meeting_minutes,
                "location_type": location_type,
                "calendar_gap_minutes": calendar_gap_minutes,
                "meeting_density_today": meeting_density_today,
                "minutes_since_last_meeting": minutes_since_last_meeting,
                "surfaces_today": surfaces_today,
                "time_since_last_surface_minutes": time_since_last_surface_minutes,
                "email_themes": email_themes,
                "communication_burst": communication_burst,
                "lat": lat,
                "lng": lng,
            }.items()
            if v is not None
        }
        snapshot = base.model_copy(update=updates)
        if updates:
            sources = list(snapshot.sensor_sources)
            if "manual" not in sources:
                sources.append("manual")
            snapshot = snapshot.model_copy(update={"sensor_sources": sources})
            snapshot = enrich_modes(snapshot)
        return await write_context(snapshot, user_id=settings.kairos_user_id)

    result = asyncio.run(_run())
    return {"status": "ok", "context": result.model_dump(mode="json")}


# Policy tools for ADK agent + FastMCP (no direct OS delivery tool)
ALL_TOOLS = [
    connect_google,
    start_google_connect,
    wait_google_connect,
    google_connect_status,
    sync_google_headspace,
    get_current_context,
    fuse_headspace_context,
    set_context,
    get_relevant_bookmarks,
    get_cluster_summary,
    run_heartbeat,
    record_feedback,
]
