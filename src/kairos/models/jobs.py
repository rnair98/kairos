"""Background prep job models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

PrepJobStatus = Literal["pending", "running", "done", "failed"]
JobBackend = Literal["local", "arq"]


class PrepJobParams(BaseModel):
    """Parameters for bookmark prep (CLI, API, Arq)."""

    sync: bool = False
    max_pages: int | None = None
    skip_enrich: bool = False
    skip_research: bool = False
    skip_embed: bool = False
    skip_cluster: bool = False
    research_limit: int | None = None
    research_concurrency: int | None = None
    clustered_only: bool = False

    def research_clustered_only(self) -> bool:
        return self.clustered_only


class PrepJobResult(BaseModel):
    """Serialized pipeline stage outputs stored on completed jobs."""

    sync: dict[str, Any] | None = None
    enrich: dict[str, Any] = Field(default_factory=dict)
    research: dict[str, Any] | None = None
    embed: dict[str, Any] = Field(default_factory=dict)
    cluster: dict[str, Any] | None = None
    cluster_skipped: bool = False
    cluster_skip_reason: str | None = None


class PrepJobRecord(BaseModel):
    """Database-backed prep job document."""

    job_id: str
    status: PrepJobStatus = "pending"
    params: PrepJobParams = Field(default_factory=PrepJobParams)
    created_at: datetime
    updated_at: datetime
    result: PrepJobResult | None = None
    error: str | None = None


class PrepJobStartResponse(BaseModel):
    job_id: str
    status: PrepJobStatus = "pending"
    backend: str
