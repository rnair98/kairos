"""Shared Pydantic models for Kairos data shapes."""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

HeartbeatStatus = Literal["KAIROS_OK", "SURFACE"]
DeliveryMode = Literal["auto", "return_only", "none"]
NotificationStatus = Literal["pending", "snoozed", "dismissed", "acted", "expired"]
FeedbackAction = Literal["expanded", "link_click", "snoozed", "dismissed", "acted", "ignored"]
TopicalAffinity = Literal["work", "explore", "recovery", "triage"]
AttentionCapacity = Literal["high", "medium", "low", "none"]
LocationType = Literal["desk", "commute", "gym", "cafe", "near_anchor", "unknown"]


class ContextSnapshot(BaseModel):
    """Live headspace vector at decision time (PLAN.md)."""

    upcoming_event_title: str | None = None
    recent_event_title: str | None = None
    post_meeting_minutes: int | None = None
    location_type: LocationType = "unknown"

    calendar_gap_minutes: int = 0
    meeting_density_today: float = 0.0
    minutes_since_last_meeting: int = 0
    surfaces_today: int = 0
    time_since_last_surface_minutes: int = 0

    hour: int = Field(default_factory=lambda: datetime.now().hour)
    day_of_week: int = Field(default_factory=lambda: datetime.now().weekday())
    is_weekend: bool = False

    # Fused sensor layer (HeadspaceComposer)
    lat: float | None = None
    lng: float | None = None
    email_themes: list[str] = Field(default_factory=list)
    communication_burst: bool = False
    topical_affinity: TopicalAffinity | None = None
    attention_capacity: AttentionCapacity | None = None
    sensor_sources: list[str] = Field(default_factory=list)
    fused_at: datetime | None = None
    moment_narrative: str | None = None
    """LLM-composed headspace narrative used as the ranking query (replaces template moment_text)."""
    moment_narrative_at: datetime | None = None
    last_surface_at: datetime | None = None
    surface_budget_day: str | None = None
    """UTC date (YYYY-MM-DD) for resetting surfaces_today."""


class BookmarkEnrichment(BaseModel):
    """LLM-derived metadata for a bookmark at ingest."""

    topic_tags: list[str]
    consumption_mode: Literal["read-deep", "skim", "watch", "act-in-world", "save-to-project"]
    energy_cost: float = Field(ge=0.0, le=1.0)
    geo_anchor: str | None = None
    perishability: Literal["evergreen", "dated", "time-sensitive"] = "evergreen"


class UrlCitation(BaseModel):
    """Source link from Exa web grounding."""

    url: str
    title: str | None = None
    cited_text: str | None = None


class DigestSourceLink(BaseModel):
    """Grounding or research source attached to a digest link card."""

    url: str
    title: str | None = None


class DigestLinkCard(BaseModel):
    """Rich bookmark link surfaced inside a cluster digest."""

    url: str
    label: str
    title: str
    consumption_mode: str = "skim"
    summary: str | None = None
    excerpt: str | None = None
    author: str | None = None
    tags: list[str] = Field(default_factory=list)
    signal: str | None = None
    status: str | None = None
    perishability: str | None = None
    energy_cost: float | None = None
    sources: list[DigestSourceLink] = Field(default_factory=list)
    researched: bool | None = None
    link_fetched: bool | None = None
    pending_research: bool | None = None


RelevanceStatus = Literal["current", "dated", "stale", "unknown"]


class BookmarkResearch(BaseModel):
    """Upfront web research on a bookmark — so the user can judge relevance fast."""

    research_summary: str  # what this link is and why it mattered, 1–2 sentences
    relevance_signal: str  # validation line, e.g. "Still the canonical Raft explainer"
    relevance_status: RelevanceStatus = "unknown"
    research_sources: list[UrlCitation] = Field(default_factory=list)


class BookmarkDocument(BaseModel):
    """Bookmark document stored in the database (X API + optional enrichment)."""

    id: str | None = Field(default=None, alias="_id")
    x_tweet_id: str
    url: str
    raw_text: str
    author_id: str | None = None
    author_username: str | None = None
    tweet_created_at: datetime | None = None
    context_annotations: list[dict] = Field(default_factory=list)
    referenced_tweets: list[dict] = Field(default_factory=list)

    embedding: list[float] | None = None
    cluster_id: str | None = None
    topic_tags: list[str] = Field(default_factory=list)
    consumption_mode: Literal[
        "read-deep", "skim", "watch", "act-in-world", "save-to-project"
    ] | None = None
    energy_cost: float | None = Field(default=None, ge=0.0, le=1.0)
    geo_anchor: str | None = None
    geo_coords: list[float] | None = None
    perishability: Literal["evergreen", "dated", "time-sensitive"] | None = None

    # Upfront web research (kairos bookmarks research)
    research_summary: str | None = None
    relevance_signal: str | None = None
    relevance_status: RelevanceStatus | None = None
    research_sources: list[UrlCitation] = Field(default_factory=list)
    researched_at: datetime | None = None
    research_source_hash: str | None = None

    # Fetched destination page (link article — not X API)
    link_final_url: str | None = None
    link_title: str | None = None
    link_description: str | None = None
    link_body_excerpt: str | None = None
    link_fetched_at: datetime | None = None
    link_fetch_error: str | None = None

    ingested_at: datetime | None = None
    last_synced_at: datetime | None = None
    last_surfaced_at: datetime | None = None
    surface_count: int = 0

    model_config = {"populate_by_name": True}


class HeadspaceEnrichment(BaseModel):
    """LLM interpretation of fused sensors for ranking and gating."""

    topical_affinity: TopicalAffinity | None = None
    attention_capacity: AttentionCapacity | None = None
    email_themes: list[str] = Field(default_factory=list)
    communication_burst: bool = False
    moment_narrative: str = ""
    headspace_summary: str | None = None


class MomentFitResult(BaseModel):
    """LLM check: does the winning cluster fit this moment before we generate digest?"""

    fit: bool
    reason: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)


class DigestCritique(BaseModel):
    """LLM critique of a draft digest before final revision."""

    strong_enough: bool
    issues: list[str] = Field(default_factory=list)
    revision_hints: str = ""


class ClusterLabel(BaseModel):
    """LLM-generated cluster name and summary at index time."""

    name: str
    summary: str
    evergreen: bool = False


class ClusterDigestCore(BaseModel):
    """Structured digest fields generated from bookmarks + context."""

    cluster_id: str
    cluster_name: str
    summary: str
    why_now: str
    links: list[DigestLinkCard] = Field(default_factory=list)
    member_count: int


class ClusterDigest(ClusterDigestCore):
    """Topic cluster digest surfaced to the user."""

    web_context: str | None = None
    citations: list[UrlCitation] = Field(default_factory=list)
    digest_style: str = "standard"
    """Treatment bucket for GAMBITTS-lite: evergreen | grounded | context_primed | standard."""


class SurfaceDecision(BaseModel):
    """Result of the ranking pipeline + interrupt gate."""

    should_surface: bool
    cluster_id: str | None = None
    digest: ClusterDigest | None = None
    gate_reasons: dict[str, bool] = Field(default_factory=dict)
    adjusted_score: float | None = None
    context: ContextSnapshot | None = None


class NotificationRecord(BaseModel):
    """Canonical persisted surface event (delivery adapters fan out from this)."""

    notification_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str | None = None
    cluster_id: str | None = None
    digest: ClusterDigest | None = None
    context_snapshot: ContextSnapshot | None = None
    status: NotificationStatus = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None


class DeliveryHints(BaseModel):
    """Instructions for host agents (MCP clients) on how to present a surface."""

    rendered_markdown: str = ""
    dashboard_url: str | None = None
    suggested_host_actions: list[str] = Field(default_factory=list)
    suppress_ok_in_chat: bool = True


class HeartbeatResult(BaseModel):
    """Structured heartbeat contract — same shape for HTTP, MCP, and ADK agent."""

    status: HeartbeatStatus
    decision: SurfaceDecision
    notification: NotificationRecord | None = None
    delivery: DeliveryHints | None = None
    activity: list[str] = Field(default_factory=list)
    reason: str | None = None


class FeedbackRequest(BaseModel):
    """Host-reported interaction with a surfaced digest."""

    notification_id: str
    action: FeedbackAction
    url: str | None = None


class HeartbeatRequest(BaseModel):
    """Web/API heartbeat — direct policy by default; opt into ADK sensor fusion."""

    context_override: str | None = None
    via_agent: bool | None = None
    """When null, falls back to HEARTBEAT_DEFAULT_VIA_AGENT."""
