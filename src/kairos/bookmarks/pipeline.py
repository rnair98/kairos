"""Incremental bookmark pipeline — sync, enrich, research, embed, cluster."""

from __future__ import annotations

from dataclasses import dataclass, field

from kairos.bookmarks.enrich import EnrichResult, enrich_stored_bookmarks
from kairos.bookmarks.index import ClusterResult, EmbedResult, cluster_stored_bookmarks, embed_stored_bookmarks
from kairos.bookmarks.research import ResearchResult, research_stored_bookmarks
from kairos.db.bookmarks import count_unclustered_embedded
from kairos.ingest.sync import SyncResult, sync_bookmarks_from_x


@dataclass
class PipelineResult:
    sync: SyncResult | None = None
    enrich: EnrichResult = field(default_factory=EnrichResult)
    research: ResearchResult | None = None
    embed: EmbedResult = field(default_factory=EmbedResult)
    cluster: ClusterResult | None = None
    cluster_skipped: bool = False
    cluster_skip_reason: str | None = None


async def run_bookmark_prep(
    *,
    sync: bool = False,
    incremental_sync: bool = True,
    max_pages: int | None = None,
    skip_enrich: bool = False,
    skip_research: bool = False,
    skip_embed: bool = False,
    skip_cluster: bool = False,
    recluster_if_stale: bool = True,
    enrich_concurrency: int | None = None,
    research_limit: int | None = None,
    research_concurrency: int | None = None,
    research_clustered_only: bool | None = None,
    embed_limit: int | None = None,
) -> PipelineResult:
    """Single entry: enrich → research → embed → cluster (optional X sync first)."""
    sync_result: SyncResult | None = None
    if sync:
        sync_result = await sync_bookmarks_from_x(
            max_pages=max_pages,
            incremental=incremental_sync,
            enrich=False,
            enrich_concurrency=enrich_concurrency,
            close_after=False,
        )

    enrich = EnrichResult()
    if not skip_enrich:
        enrich = await enrich_stored_bookmarks(concurrency=enrich_concurrency)

    research: ResearchResult | None = None
    if not skip_research:
        research = await research_stored_bookmarks(
            limit=research_limit,
            concurrency=research_concurrency,
            clustered_only=research_clustered_only,
        )

    embed = EmbedResult()
    if not skip_embed:
        embed = await embed_stored_bookmarks(limit=embed_limit)

    cluster: ClusterResult | None = None
    cluster_skipped = False
    cluster_skip_reason: str | None = None

    if skip_cluster:
        cluster_skipped = True
        cluster_skip_reason = "skip_cluster flag"
    elif recluster_if_stale:
        unclustered = await count_unclustered_embedded()
        new_vectors = embed.embedded > 0
        if unclustered > 0 or new_vectors:
            cluster = await cluster_stored_bookmarks()
        else:
            cluster_skipped = True
            cluster_skip_reason = "no new embeddings or unclustered bookmarks"
    else:
        cluster = await cluster_stored_bookmarks()

    return PipelineResult(
        sync=sync_result,
        enrich=enrich,
        research=research,
        embed=embed,
        cluster=cluster,
        cluster_skipped=cluster_skipped,
        cluster_skip_reason=cluster_skip_reason,
    )


async def run_incremental_pipeline(
    *,
    incremental_sync: bool = True,
    max_pages: int | None = None,
    skip_enrich: bool = False,
    skip_embed: bool = False,
    skip_cluster: bool = False,
    recluster_if_stale: bool = True,
    enrich_concurrency: int | None = None,
) -> PipelineResult:
    """Fetch new/changed bookmarks from X, then process only stale rows in the database."""
    return await run_bookmark_prep(
        sync=True,
        incremental_sync=incremental_sync,
        max_pages=max_pages,
        skip_enrich=skip_enrich,
        skip_research=True,
        skip_embed=skip_embed,
        skip_cluster=skip_cluster,
        recluster_if_stale=recluster_if_stale,
        enrich_concurrency=enrich_concurrency,
    )
