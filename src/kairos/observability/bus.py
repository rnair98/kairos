"""In-process event bus for SSE dashboard observability."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import orjson


@dataclass
class AgentEvent:
    timestamp: datetime
    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        payload = {
            "timestamp": self.timestamp.isoformat(),
            "kind": self.kind,
            "message": self.message,
            "data": self.data,
        }
        return f"data: {orjson.dumps(payload).decode()}\n\n"

    def dedupe_key(self) -> tuple[str, str, str]:
        return (self.timestamp.isoformat(), self.kind, self.message)


class EventBus:
    """Async pub/sub with optional database persistence for cross-process SSE."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[AgentEvent]] = []
        self._history: list[AgentEvent] = []
        self._max_history = 500

    def emit(self, kind: str, message: str, **data: Any) -> AgentEvent:
        event = AgentEvent(
            timestamp=datetime.now(timezone.utc),
            kind=kind,
            message=message,
            data=data,
        )
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]
        for queue in self._subscribers:
            queue.put_nowait(event)
        try:
            from kairos.observability.logging import log_pipeline_event

            log_pipeline_event(kind=kind, message=message, **data)
        except Exception:  # noqa: BLE001 — logging must never break the bus
            pass
        self._schedule_persist(event)
        return event

    def _schedule_persist(self, event: AgentEvent) -> None:
        try:
            from kairos.config import settings

            if not settings.event_persist_enabled:
                return
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._persist_sync(event)
            return

        loop.create_task(self._persist_async(event))

    async def _persist_async(self, event: AgentEvent) -> None:
        try:
            from kairos.db.events import insert_pipeline_event

            await insert_pipeline_event(event)
        except Exception:  # noqa: BLE001
            pass

    def _persist_sync(self, event: AgentEvent) -> None:
        try:
            asyncio.run(self._persist_async(event))
        except Exception:  # noqa: BLE001
            pass

    def subscribe(self) -> asyncio.Queue[AgentEvent]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[AgentEvent]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    @property
    def history(self) -> list[AgentEvent]:
        return list(self._history)

    async def _replay_events(self) -> list[AgentEvent]:
        from kairos.config import settings

        if not settings.event_persist_enabled:
            return list(self._history)

        try:
            from kairos.db.events import list_recent_events

            persisted = await list_recent_events(limit=self._max_history)
        except Exception:  # noqa: BLE001
            return list(self._history)

        merged: list[AgentEvent] = []
        seen: set[tuple[str, str, str]] = set()
        for event in persisted + self._history:
            key = event.dedupe_key()
            if key in seen:
                continue
            seen.add(key)
            merged.append(event)
        merged.sort(key=lambda e: e.timestamp)
        return merged[-self._max_history :]

    async def stream(self) -> AsyncIterator[AgentEvent]:
        queue = self.subscribe()
        try:
            for past in await self._replay_events():
                yield past
            while True:
                yield await queue.get()
        finally:
            self.unsubscribe(queue)


event_bus = EventBus()
