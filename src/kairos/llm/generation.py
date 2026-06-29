"""Structured LLM calls via the Gemini Interactions API."""

from __future__ import annotations

from typing import Any

import orjson

from kairos.config import settings
from kairos.llm.interactions import create_interaction
from kairos.llm.grounding import parse_grounded_interaction
from kairos.models.schemas import (
    BookmarkEnrichment,
    ClusterDigest,
    ClusterDigestCore,
    ClusterLabel,
    ContextSnapshot,
    DigestCritique,
)
from kairos.observability.bus import event_bus


def _inline_refs(node: Any, defs: dict) -> Any:
    """Recursively inline ``$ref`` pointers against ``$defs``.

    Pydantic emits nested models (e.g. ClusterDigestCore.links → DigestLinkCard)
    into ``$defs`` and references them with ``{"$ref": "#/$defs/Name"}``. The
    Gemini Interactions API response_format has no ``$defs`` section, so any ref
    left in ``properties`` resolves to nothing ("reference to undefined schema").
    Inlining makes the schema self-contained.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if ref:
            name = ref.split("/")[-1]
            resolved = _inline_refs(defs.get(name, {}), defs)
            siblings = {k: _inline_refs(v, defs) for k, v in node.items() if k != "$ref"}
            return {**resolved, **siblings}
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    return node


def _response_format_for(model: type) -> list[dict]:
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    properties = _inline_refs(schema.get("properties", {}), defs)
    fmt: dict = {"type": "object", "properties": properties}
    if required := schema.get("required"):
        fmt["required"] = required
    return [fmt]


_BOOKMARK_ENRICHMENT_FORMAT = _response_format_for(BookmarkEnrichment)
_DIGEST_CRITIQUE_FORMAT = _response_format_for(DigestCritique)
_DIGEST_CORE_FORMAT = _response_format_for(ClusterDigestCore)


_CLUSTER_LABEL_FORMAT = _response_format_for(ClusterLabel)


def label_cluster(members: list[dict]) -> ClusterLabel:
    """Generate human-readable cluster name + summary from member bookmarks."""
    snippets = "\n".join(
        f"- {(doc.get('raw_text') or '')[:200]}" for doc in members[:6] if doc.get("raw_text")
    )
    tags: list[str] = []
    for doc in members:
        tags.extend(doc.get("topic_tags") or [])
    tag_line = ", ".join(sorted(set(tags))[:12])
    interaction = create_interaction(
        label="cluster-label",
        model=settings.gemini_flash_lite_model,
        input=(
            f"Topic tags: {tag_line or '(none)'}\n\n"
            f"Bookmark samples:\n{snippets or '(empty)'}\n\n"
            "Name this cluster (short, specific) and write a one-sentence summary."
        ),
        system_instruction=(
            "You label bookmark topic clusters for a personal knowledge agent. "
            "Name should be 2-5 words; summary captures the shared theme. "
            "evergreen=true for timeless reference material (architecture, fundamentals, "
            "evergreen tutorials) that does not benefit from news-style web grounding."
        ),
        response_format=_CLUSTER_LABEL_FORMAT,
        store=False,
    )
    payload = interaction.output_text or '{"name": "Cluster", "summary": ""}'
    return ClusterLabel.model_validate_json(payload)


def enrich_bookmark(raw_text: str, url: str) -> BookmarkEnrichment:
    """Extract bookmark metadata via structured output."""
    max_chars = settings.enrich_max_input_chars
    interaction = create_interaction(
        label="bookmark-enrich",
        model=settings.gemini_flash_lite_model,
        input=(
            "Classify this bookmark.\n\n"
            f"URL: {url}\n\n"
            f"Content:\n{raw_text[:max_chars]}"
        ),
        system_instruction=(
            "Classify bookmarks for a personal knowledge agent. "
            "energy_cost: 0=quick skim, 1=deep focus. "
            "geo_anchor only for explicit place/product mentions."
        ),
        response_format=_BOOKMARK_ENRICHMENT_FORMAT,
        store=False,
    )
    payload = interaction.output_text or "{}"
    return BookmarkEnrichment.model_validate_json(payload)


_DEFAULT_DIGEST_PROMPT = (
    "Write a cluster digest for surfacing via web inbox or host agent chat. "
    "why_now: one line explaining timing fit (gap, location, calendar). "
    "links: up to 5 entries with url placeholder '#', label, consumption_mode."
)


def _structured_cluster_digest(
    cluster_id: str,
    cluster_name: str,
    cluster_summary: str,
    bookmark_snippets: list[str],
    context: ContextSnapshot,
    member_count: int,
    *,
    prompt_override: str | None = None,
) -> ClusterDigest:
    """Core digest fields from bookmarks + context (no web search)."""
    context_json = context.model_dump_json()
    snippets = "\n".join(f"- {s[:300]}" for s in bookmark_snippets[:8])

    interaction = create_interaction(
        label="digest-core",
        model=settings.gemini_model,
        input=(
            f"Cluster: {cluster_name}\n"
            f"Summary: {cluster_summary}\n\n"
            f"Bookmarks:\n{snippets}\n\n"
            f"Context:\n{context_json}"
        ),
        system_instruction=prompt_override or _DEFAULT_DIGEST_PROMPT,
        response_format=_DIGEST_CORE_FORMAT,
        store=False,
    )
    payload = interaction.output_text or "{}"
    data = orjson.loads(payload)
    data.setdefault("cluster_id", cluster_id)
    data.setdefault("cluster_name", cluster_name)
    data.setdefault("member_count", member_count)
    return ClusterDigest.model_validate(data)


def _ground_digest_with_search(
    digest: ClusterDigest,
    context: ContextSnapshot,
    bookmark_snippets: list[str],
) -> ClusterDigest:
    """Enrich digest with timely web context via Google Search grounding."""
    snippets = "\n".join(f"- {s[:200]}" for s in bookmark_snippets[:5])
    context_bits = [
        f"location={context.location_type}",
        f"calendar_gap_minutes={context.calendar_gap_minutes}",
    ]
    if context.upcoming_event_title:
        context_bits.append(f"upcoming_event={context.upcoming_event_title}")
    if context.recent_event_title:
        context_bits.append(f"recent_event={context.recent_event_title}")

    interaction = create_interaction(
        label="digest-ground",
        model=settings.gemini_model,
        input=(
            f"Cluster topic: {digest.cluster_name}\n"
            f"Digest summary: {digest.summary}\n"
            f"Why now (draft): {digest.why_now}\n\n"
            f"Saved bookmarks:\n{snippets or '(none)'}\n\n"
            f"User moment: {', '.join(context_bits)}\n\n"
            "Search the web for timely context that connects this cluster to the user's "
            "current moment. Write 2-3 sentences on what's happening in this topic space "
            "that makes revisiting these bookmarks worthwhile now. "
            "Do not list the bookmarks again."
        ),
        system_instruction=(
            "You enrich a personal bookmark digest with grounded, current web context. "
            "Be concise and factual. Prefer recent developments over generic background."
        ),
        tools=[{"type": "google_search"}],
        store=False,
    )
    grounded = parse_grounded_interaction(interaction)
    if not grounded.text:
        return digest

    return digest.model_copy(
        update={
            "web_context": grounded.text,
            "citations": grounded.citations,
        }
    )


def _critique_digest(
    digest: ClusterDigest,
    context: ContextSnapshot,
    bookmark_snippets: list[str],
) -> DigestCritique:
    """LLM critique of draft digest quality and moment fit."""
    snippets = "\n".join(f"- {s[:200]}" for s in bookmark_snippets[:5])
    interaction = create_interaction(
        label="digest-critique",
        model=settings.gemini_flash_lite_model,
        input=(
            f"Draft digest:\n{digest.model_dump_json()}\n\n"
            f"User moment:\n{context.model_dump_json()}\n\n"
            f"Bookmarks:\n{snippets or '(none)'}\n\n"
            "Critique whether this digest is strong enough to interrupt the user. "
            "strong_enough=false if why_now is generic, summary is vague, or timing "
            "connection is weak."
        ),
        system_instruction=(
            "Be a harsh editor. List specific issues and revision_hints for improvement."
        ),
        response_format=_DIGEST_CRITIQUE_FORMAT,
        store=False,
    )
    payload = interaction.output_text or '{"strong_enough": true, "issues": [], "revision_hints": ""}'
    critique = DigestCritique.model_validate_json(payload)
    if critique.strong_enough:
        critique_msg = "Digest critique passed — strong enough to interrupt."
    else:
        issues = "; ".join(critique.issues[:3]) if critique.issues else "needs revision"
        critique_msg = f"Digest critique flagged issues — {issues}."
    event_bus.emit(
        "intelligence",
        critique_msg,
        strong_enough=critique.strong_enough,
        issues=critique.issues,
    )
    return critique


def _revise_digest(
    digest: ClusterDigest,
    critique: DigestCritique,
    context: ContextSnapshot,
    bookmark_snippets: list[str],
) -> ClusterDigest:
    """Revise digest using critique feedback."""
    snippets = "\n".join(f"- {s[:300]}" for s in bookmark_snippets[:8])
    interaction = create_interaction(
        label="digest-revise",
        model=settings.gemini_model,
        input=(
            f"Original digest:\n{digest.model_dump_json()}\n\n"
            f"Critique issues: {critique.issues}\n"
            f"Revision hints: {critique.revision_hints}\n\n"
            f"User moment:\n{context.model_dump_json()}\n\n"
            f"Bookmarks:\n{snippets}\n\n"
            "Revise summary and why_now to address the critique. Keep links unchanged."
        ),
        system_instruction=(
            "Produce a sharper, more moment-specific digest. why_now must cite concrete "
            "timing signals (gap, location, calendar, email themes)."
        ),
        response_format=_DIGEST_CORE_FORMAT,
        store=False,
    )
    payload = interaction.output_text or "{}"
    data = orjson.loads(payload)
    data.setdefault("cluster_id", digest.cluster_id)
    data.setdefault("cluster_name", digest.cluster_name)
    data.setdefault("member_count", digest.member_count)
    revised = ClusterDigest.model_validate(
        {**digest.model_dump(), **data, "web_context": digest.web_context, "citations": digest.citations}
    )
    event_bus.emit("intelligence", "Revised digest after critique to sharpen timing and relevance.")
    return revised


def _infer_digest_style(
    digest: ClusterDigest,
    context: ContextSnapshot,
    evergreen: bool,
    was_grounded: bool,
    was_revised: bool,
) -> str:
    """Derive GAMBITTS treatment bucket from digest characteristics."""
    if evergreen and not was_grounded:
        return "evergreen"
    if was_grounded:
        return "grounded"
    if was_revised:
        return "revised"
    if context.upcoming_event_title or context.topical_affinity == "work":
        return "context_primed"
    return "standard"


def generate_cluster_digest(
    cluster_id: str,
    cluster_name: str,
    cluster_summary: str,
    bookmark_snippets: list[str],
    context: ContextSnapshot,
    *,
    member_count: int = 0,
    evergreen: bool = False,
    prompt_override: str | None = None,
) -> ClusterDigest:
    """Compose cluster digest — fast single call or multi-step with search/critique."""
    if settings.intelligence_digest_runtime_fast:
        return _structured_cluster_digest(
            cluster_id,
            cluster_name,
            cluster_summary,
            bookmark_snippets,
            context,
            member_count,
            prompt_override=prompt_override,
        )

    digest = _structured_cluster_digest(
        cluster_id,
        cluster_name,
        cluster_summary,
        bookmark_snippets,
        context,
        member_count,
        prompt_override=prompt_override,
    )
    skip_search = evergreen and settings.digest_skip_search_evergreen
    was_grounded = False
    if settings.digest_use_google_search and not skip_search:
        digest = _ground_digest_with_search(digest, context, bookmark_snippets)
        was_grounded = bool(digest.web_context)
    was_revised = False
    if settings.intelligence_digest_multistep:
        critique = _critique_digest(digest, context, bookmark_snippets)
        if not critique.strong_enough and critique.revision_hints:
            digest = _revise_digest(digest, critique, context, bookmark_snippets)
            was_revised = True
    style = _infer_digest_style(digest, context, evergreen, was_grounded, was_revised)
    return digest.model_copy(update={"digest_style": style})
