"""Read and write live headspace context."""

from __future__ import annotations

import asyncio

from kairos.config import settings
from kairos.core.headspace import fuse_headspace
from kairos.db.context_cache import load_context, save_context
from kairos.db.engine import close_db
from kairos.models.schemas import ContextSnapshot
from kairos.observability.bus import event_bus
from kairos.observability.narrate import describe_headspace_read, describe_headspace_update

_memory_context: dict[str, ContextSnapshot] = {}


def _default_stub() -> ContextSnapshot:
    """Demo persona when no fused snapshot exists."""
    return fuse_headspace(
        location_type=settings.kairos_location_type or "cafe",
        calendar_gap_minutes=90,
        meeting_density_today=0.3,
        time_since_last_surface_minutes=120,
        sensor_sources=["demo_stub"],
    )


def is_demo_context(context: ContextSnapshot) -> bool:
    """True when headspace is the demo stub (no live sensors synced)."""
    return "demo_stub" in context.sensor_sources


def context_meta(context: ContextSnapshot) -> dict[str, bool | str | None]:
    """Metadata for API responses."""
    return {
        "is_demo_stub": is_demo_context(context),
        "sensor_sources": list(context.sensor_sources),
    }


async def get_context_async(user_id: str | None = None) -> ContextSnapshot:
    cache_key = user_id or "__default__"
    if cache_key in _memory_context:
        return _memory_context[cache_key]
    try:
        cached = await load_context(user_id)
        if cached is not None:
            _memory_context[cache_key] = cached
            return cached
    finally:
        await close_db()
    return _default_stub()


def read_context(user_id: str | None = None) -> ContextSnapshot:
    """Return the latest fused headspace snapshot for a user."""
    ctx = asyncio.run(get_context_async(user_id))
    event_bus.emit(
        "context",
        describe_headspace_read(ctx),
        user_id=user_id,
        context=ctx.model_dump(mode="json"),
    )
    return ctx


async def write_context(
    snapshot: ContextSnapshot,
    *,
    user_id: str | None = None,
) -> ContextSnapshot:
    """Persist and hot-cache a fused snapshot."""
    cache_key = user_id or "__default__"
    _memory_context[cache_key] = snapshot
    try:
        await save_context(snapshot, user_id=user_id)
    finally:
        await close_db()
    event_bus.emit(
        "context",
        describe_headspace_update(snapshot),
        user_id=user_id,
        context=snapshot.model_dump(mode="json"),
    )
    return snapshot


def clear_memory_context() -> None:
    """Test helper — reset in-process cache."""
    _memory_context.clear()
