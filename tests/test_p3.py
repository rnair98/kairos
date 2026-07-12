"""P3 feature tests — sensors, metrics scoping helpers."""

from __future__ import annotations

from kairos.db.metrics import _user_where
from kairos.models.sensors import (
    CalendarEvent,
    EmailThread,
    FuseHeadspacePayload,
    calendar_events_to_dicts,
    email_threads_to_dicts,
)


def test_calendar_event_model_and_dict_roundtrip():
    event = CalendarEvent(summary="Standup", start={"dateTime": "2026-06-28T10:00:00Z"})
    d = calendar_events_to_dicts([event])[0]
    assert d["summary"] == "Standup"
    assert CalendarEvent.model_validate(d).summary == "Standup"


def test_email_thread_model():
    thread = EmailThread(subject="Deploy review", snippet="Can we ship today?")
    d = email_threads_to_dicts([thread])[0]
    assert d["subject"] == "Deploy review"


def test_fuse_headspace_payload_empty_lists():
    payload = FuseHeadspacePayload()
    assert payload.calendar_events == []
    assert payload.email_threads == []


def test_metrics_user_match_demo_gym_aggregate():
    assert _user_where(None, include_sim=True) == ("sim = 1", [])


def test_metrics_user_match_scoped_user():
    assert _user_where("user-123", include_sim=False) == ("user_id = ?", ["user-123"])
