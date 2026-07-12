"""Ranking pipeline + interrupt gate (policy core)."""

from __future__ import annotations

import asyncio
import logging

from kairos.bookmarks.urls import bookmark_snippet_text, build_bookmark_link_card
from kairos.models.schemas import DigestLinkCard
from kairos.config import settings
from kairos.core.bandit import thompson_sample
from kairos.core.moment import context_class, moment_text
from kairos.db.bandit import ensure_bandit_indexes, get_bandit_params_batch
from kairos.db.bookmarks import list_bookmarks_by_cluster
from kairos.db.clusters import list_clusters
from kairos.db.feedback import list_snoozed_cluster_ids
from kairos.db.engine import close_db
from kairos.db.vector_search import (
    rank_clusters_in_memory,
    search_clusters_by_vector,
)
from kairos.embeddings.encoder import encode_query
from kairos.db.optimization_runs import get_active_prompt
from kairos.llm.generation import generate_cluster_digest
from kairos.models.schemas import ClusterDigest, ContextSnapshot, SurfaceDecision
from kairos.observability.bus import event_bus
from kairos.observability.narrate import describe_gate_failures, describe_ranking_complete

logger = logging.getLogger(__name__)


def _gate_reasons(
    context: ContextSnapshot,
    *,
    adjusted_score: float | None,
) -> dict[str, bool]:
    threshold = settings.surface_score_threshold
    return {
        "daily_budget": context.surfaces_today < settings.daily_surface_budget,
        "calendar_gap": context.calendar_gap_minutes >= settings.min_calendar_gap_minutes,
        "min_gap": (
            context.time_since_last_surface_minutes
            >= settings.min_gap_between_surfaces_minutes
        ),
        "score_threshold": adjusted_score is not None and adjusted_score >= threshold,
    }


def _hard_gates_pass(context: ContextSnapshot) -> bool:
    """Budget and gap checks that do not depend on cluster ranking."""
    return (
        context.surfaces_today < settings.daily_surface_budget
        and context.calendar_gap_minutes >= settings.min_calendar_gap_minutes
        and context.time_since_last_surface_minutes
        >= settings.min_gap_between_surfaces_minutes
    )


def _should_surface(gate_reasons: dict[str, bool]) -> bool:
    return all(gate_reasons.values())


def _bookmark_snippets(members: list[dict]) -> list[str]:
    snippets: list[str] = []
    for member in members:
        snippet = bookmark_snippet_text(member)
        if snippet:
            snippets.append(snippet)
    return snippets


def _merge_bookmark_links(digest: ClusterDigest, members: list[dict]) -> ClusterDigest:
    """Prefer real bookmark URLs over LLM placeholders, with card-ready metadata."""
    ranked = sorted(
        members,
        key=lambda m: (0 if m.get("research_summary") else 1),
    )
    links: list[DigestLinkCard] = []
    for doc in ranked[:5]:
        card = build_bookmark_link_card(doc)
        if card:
            links.append(card)
    if not links:
        return digest
    return digest.model_copy(update={"links": links})


async def evaluate_surface(
    context: ContextSnapshot,
    context_override: str | None = None,
    *,
    user_id: str | None = None,
    generate_digest: bool = True,
    _keep_db_open: bool = False,
) -> SurfaceDecision:
    """Run feasibility → vector match → bandit → interrupt gate → (optional) digest.

    generate_digest=False skips the LLM call — used by the gym to avoid
    10–25s latency across thousands of ticks.
    _keep_db_open=True skips close_db() — used by the gym to reuse the
    database connection across the full run.
    """
    if context_override:
        snippet = context_override[:120] + ("…" if len(context_override) > 120 else "")
        event_bus.emit(
            "pipeline",
            f"Applying context override for this cycle: \"{snippet}\"",
            override=context_override[:120],
        )

    try:
        await ensure_bandit_indexes()
        clusters = await list_clusters()
        clusters = [c for c in clusters if c.get("centroid_embedding")]
        event_bus.emit(
            "pipeline",
            f"Loaded {len(clusters)} bookmark clusters with embeddings.",
            total=len(clusters),
        )
        ctx_class = context_class(context)
        snoozed_ids = set(await list_snoozed_cluster_ids(ctx_class, user_id=user_id))
        if snoozed_ids:
            clusters = [c for c in clusters if c.get("cluster_id") not in snoozed_ids]
            event_bus.emit(
                "pipeline",
                f"Filtered {len(snoozed_ids)} snoozed clusters — {len(clusters)} candidates remain.",
                snoozed=len(snoozed_ids),
                remaining=len(clusters),
            )

        best_vector = 0.0
        best_adjusted = 0.0
        best_cluster: dict | None = None
        digest: ClusterDigest | None = None

        hard_gates = _hard_gates_pass(context)
        if hard_gates:
            event_bus.emit("pipeline", "Feasibility gates passed — calendar gap and daily budget look good.")
        else:
            event_bus.emit(
                "pipeline",
                f"Feasibility gates blocked ranking — {describe_gate_failures(_gate_reasons(context, adjusted_score=None))}.",
                passed=False,
            )
        if clusters and hard_gates:
            query = moment_text(context, context_override)
            moment_vector = await asyncio.to_thread(encode_query, query)

            vector_hits = await search_clusters_by_vector(
                moment_vector,
                limit=len(clusters),
                exclude_cluster_ids=snoozed_ids or None,
            )
            used_atlas = bool(vector_hits)
            if not vector_hits:
                vector_hits = rank_clusters_in_memory(moment_vector, clusters)

            top_score = round(vector_hits[0][1], 3) if vector_hits else 0
            backend = "Atlas vector search" if used_atlas else "in-memory fallback"
            event_bus.emit(
                "pipeline",
                f"Embedded the current moment and ranked {len(vector_hits)} clusters via {backend} "
                f"(top score {top_score:.2f}).",
                candidates=len(vector_hits),
                top_score=top_score,
            )

            cluster_ids = [
                cluster["cluster_id"]
                for cluster, _ in vector_hits
                if cluster.get("cluster_id")
            ]
            bandit_by_cluster = await get_bandit_params_batch(
                cluster_ids, ctx_class, user_id=user_id
            )

            for cluster, vector_score in vector_hits:
                cluster_id = cluster.get("cluster_id")
                if not cluster_id:
                    continue
                params = bandit_by_cluster[cluster_id]
                bandit_weight = thompson_sample(params["alpha"], params["beta"])
                adjusted = vector_score * bandit_weight

                if adjusted > best_adjusted:
                    best_adjusted = adjusted
                    best_vector = vector_score
                    best_cluster = cluster

            logger.info(
                "Ranked %s clusters; best=%s vector=%.3f adjusted=%.3f",
                len(clusters),
                (best_cluster or {}).get("name"),
                best_vector,
                best_adjusted,
            )
            if best_cluster:
                cname = best_cluster.get("name") or best_cluster.get("cluster_id", "")[:12]
                event_bus.emit(
                    "pipeline",
                    f"Thompson sampling picked «{cname}» — vector {best_vector:.2f}, "
                    f"bandit-adjusted {best_adjusted:.2f}.",
                    cluster=cname,
                    vector_score=round(best_vector, 3),
                    adjusted_score=round(best_adjusted, 3),
                )

        gate_reasons = _gate_reasons(context, adjusted_score=best_adjusted if best_cluster else None)
        should_surface = _should_surface(gate_reasons) and best_cluster is not None
        failed = [k for k, v in gate_reasons.items() if not v]
        if should_surface:
            gate_msg = "Interrupt gate open — this cluster clears all checks."
        else:
            gate_msg = f"Interrupt gate closed — {describe_gate_failures(gate_reasons)}."
        event_bus.emit(
            "pipeline",
            gate_msg,
            should_surface=should_surface,
            failed_gates=failed,
        )

        members: list[dict] = []
        snippets: list[str] = []
        if should_surface and best_cluster:
            members = await list_bookmarks_by_cluster(best_cluster["cluster_id"], limit=8)
            snippets = _bookmark_snippets(members)

        if (
            should_surface
            and best_cluster
            and generate_digest
            and settings.intelligence_moment_fit_check
        ):
            from kairos.llm.compose import check_moment_fit

            fit = await asyncio.to_thread(
                check_moment_fit,
                context,
                cluster_name=best_cluster.get("name") or "Cluster",
                cluster_summary=best_cluster.get("summary") or "",
                bookmark_snippets=snippets,
            )
            gate_reasons["moment_fit"] = fit.fit and fit.confidence >= 0.4
            should_surface = _should_surface(gate_reasons)
            cname = best_cluster.get("name") or "Cluster"
            if gate_reasons["moment_fit"]:
                event_bus.emit(
                    "pipeline",
                    f"Gemini confirmed «{cname}» fits this moment (confidence {fit.confidence:.0%}).",
                    fit=fit.fit,
                    confidence=round(fit.confidence, 2),
                    passed=True,
                )
            else:
                event_bus.emit(
                    "pipeline",
                    f"Gemini rejected «{cname}» for this moment — {fit.reason or 'poor fit'}.",
                    fit=fit.fit,
                    confidence=round(fit.confidence, 2),
                    passed=False,
                    reason=fit.reason,
                )

        if should_surface and best_cluster and generate_digest:
            cname = best_cluster.get("name") or "Cluster"
            event_bus.emit(
                "pipeline",
                f"Calling Gemini to research and compose a digest for «{cname}» "
                f"from {len(snippets)} bookmarks…",
                cluster=cname,
                bookmarks=len(snippets),
            )
            active_prompt = await get_active_prompt()
            digest = await asyncio.to_thread(
                generate_cluster_digest,
                best_cluster["cluster_id"],
                best_cluster.get("name") or "Cluster",
                best_cluster.get("summary") or "",
                snippets,
                context,
                member_count=best_cluster.get("member_count") or len(members),
                evergreen=bool(best_cluster.get("evergreen")),
                prompt_override=active_prompt,
            )
            digest = _merge_bookmark_links(digest, members)
            digest = digest.model_copy(update={"cluster_id": best_cluster["cluster_id"]})
            event_bus.emit(
                "pipeline",
                f"Digest ready for «{digest.cluster_name}» — {len(digest.links or [])} links researched and validated.",
                cluster=digest.cluster_name,
                links=len(digest.links or []),
            )

        decision = SurfaceDecision(
            should_surface=should_surface,
            cluster_id=best_cluster["cluster_id"] if should_surface and best_cluster else None,
            digest=digest,
            gate_reasons=gate_reasons,
            adjusted_score=best_adjusted if best_cluster else None,
            context=context,
        )
    except RuntimeError as exc:
        logger.warning("Ranking skipped: %s", exc)
        event_bus.emit("pipeline", f"Ranking skipped — {exc}.")
        gate_reasons = _gate_reasons(context, adjusted_score=None)
        gate_reasons["score_threshold"] = False
        decision = SurfaceDecision(
            should_surface=False,
            gate_reasons=gate_reasons,
            context=context,
        )
    finally:
        if not _keep_db_open:
            await close_db()

    cluster_name = decision.digest.cluster_name if decision.digest else None
    event_bus.emit(
        "activity",
        describe_ranking_complete(
            should_surface=decision.should_surface,
            cluster_id=decision.cluster_id,
            cluster_name=cluster_name,
            adjusted_score=decision.adjusted_score,
            gate_reasons=decision.gate_reasons,
        ),
        should_surface=decision.should_surface,
        gate_reasons=decision.gate_reasons,
        cluster_id=decision.cluster_id,
        cluster_name=cluster_name,
        adjusted_score=decision.adjusted_score,
    )
    return decision
