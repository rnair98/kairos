"""FastAPI web gateway — SSE activity stream, inbox, feedback."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kairos.agent.harness import run_decision_cycle, run_decision_cycle_via_agent
from kairos.core.heartbeat import heartbeat_service
from kairos.db.bandit import bandit_user_id, ensure_bandit_indexes, list_bandit_params
from kairos.db.clusters import list_clusters
from kairos.db.metrics import get_engagement_by_day, get_overall_stats, rate_change_pct
from kairos.db.optimization_runs import get_active_prompt, list_optimization_runs
from kairos.config import settings
from kairos.core.context import context_meta, get_context_async, is_demo_context, write_context
from kairos.core.demo import DEFAULT_DEMO_OVERRIDE, reset_demo_headspace
from kairos.core.headspace import enrich_modes
from kairos.db.google_tokens import load_google_connection
from kairos.db.engine import close_db, set_db_persist
from kairos.db.notifications import list_notifications
from kairos.db.prep_jobs import create_prep_job, get_prep_job
from kairos.google.headspace_sync import fuse_and_persist_headspace
from kairos.models.jobs import PrepJobParams, PrepJobStartResponse
from kairos.models.schemas import FeedbackRequest, HeartbeatRequest
from kairos.models.sensors import FuseHeadspacePayload
from kairos.observability.bus import event_bus
from kairos.observability.logging import get_logger, setup_logging
from kairos.web.session import get_user_id

STATIC_DIR = Path(__file__).resolve().parent / "static"
log = get_logger("http")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    setup_logging()
    get_logger("app").info("Kairos web app ready")
    set_db_persist(True)
    yield
    get_logger("app").info("Kairos web app shutting down")
    set_db_persist(False)
    await close_db()


app = FastAPI(title="Kairos", version="0.1.0", lifespan=_lifespan)


@app.middleware("http")
async def structured_request_log(request: Request, call_next):
    if not settings.log_access:
        return await call_next(request)

    path = request.url.path
    method = request.method
    client = request.client.host if request.client else None
    started = time.perf_counter()
    response = await call_next(request)
    ms = round((time.perf_counter() - started) * 1000, 1)
    status = response.status_code

    bind = log.bind(method=method, path=path, status=status, ms=ms, client=client)

    if path == "/api/stream":
        bind.debug("SSE connected")
        return response

    if status >= 500:
        bind.error("{} {} → {} ({}ms)", method, path, status, ms)
    elif status >= 400:
        bind.warning("{} {} → {} ({}ms)", method, path, status, ms)
    else:
        bind.info("{} {} → {} ({}ms)", method, path, status, ms)

    return response


class DemoSurfaceRequest(BaseModel):
    context_override: str | None = None
    reset: bool = True


class SetContextRequest(BaseModel):
    upcoming_event_title: str | None = None
    recent_event_title: str | None = None
    post_meeting_minutes: int | None = None
    location_type: str | None = None
    calendar_gap_minutes: int | None = None
    meeting_density_today: float | None = None
    minutes_since_last_meeting: int | None = None
    surfaces_today: int | None = None
    time_since_last_surface_minutes: int | None = None
    email_themes: list[str] | None = None
    communication_burst: bool | None = None
    lat: float | None = None
    lng: float | None = None


def _serialize(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    if out.get("_id"):
        out["id"] = str(out.pop("_id"))
    for key in ("created_at", "updated_at", "last_updated"):
        if out.get(key) and hasattr(out[key], "isoformat"):
            out[key] = out[key].isoformat()
    return out


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/walkthrough")
async def walkthrough() -> FileResponse:
    """Animated pipeline + API map for new engineers."""
    return FileResponse(STATIC_DIR / "walkthrough.html")


@app.get("/api/stream")
async def stream_events() -> StreamingResponse:
    async def generate():
        async for event in event_bus.stream():
            yield event.to_sse()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/notifications")
async def get_notifications(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    docs = await list_notifications(limit=limit, user_id=get_user_id(request))
    return [_serialize(d) for d in docs]


@app.get("/api/bandit")
async def get_bandit_params_api(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict[str, Any]]:
    await ensure_bandit_indexes()
    docs = await list_bandit_params(limit=limit, user_id=bandit_user_id(get_user_id(request)))
    return [_serialize(d) for d in docs]


@app.post("/api/heartbeat")
async def post_heartbeat(request: Request, body: HeartbeatRequest | None = None) -> dict[str, Any]:
    payload = body or HeartbeatRequest()
    override = payload.context_override
    user_id = get_user_id(request)
    use_agent = (
        payload.via_agent
        if payload.via_agent is not None
        else settings.heartbeat_default_via_agent
    )
    if use_agent:
        result = await run_decision_cycle_via_agent(user_id=user_id)
    else:
        result = await run_decision_cycle(
            delivery="auto",
            context_override=override,
            user_id=user_id,
        )
    get_logger("http").bind(
        status=result.status,
        user_id=user_id or "anonymous",
        reason=result.reason,
        via_agent=use_agent,
    ).info("Heartbeat finished → {}", result.status)
    return result.model_dump(mode="json")


@app.post("/api/demo/surface")
async def post_demo_surface(request: Request, body: DemoSurfaceRequest | None = None) -> dict[str, Any]:
    """Reset demo headspace (optional) and run one policy cycle — SSE-friendly surface beat."""
    user_id = get_user_id(request)
    payload = body or DemoSurfaceRequest()
    if payload.reset:
        await reset_demo_headspace(user_id=user_id)
    override = payload.context_override or DEFAULT_DEMO_OVERRIDE
    result = await run_decision_cycle(
        delivery="auto",
        context_override=override,
        user_id=user_id,
    )
    get_logger("http").bind(
        status=result.status,
        user_id=user_id or "anonymous",
        reason=result.reason,
        demo=True,
    ).info("Demo surface → {}", result.status)
    return result.model_dump(mode="json")


@app.get("/api/metrics")
async def get_metrics(
    request: Request,
    days: int = Query(default=14, ge=1, le=90),
    persona: str | None = Query(default=None),
    sim: bool = Query(default=True),
) -> dict[str, Any]:
    user_id = get_user_id(request)
    by_day = await get_engagement_by_day(
        days=days, persona=persona, include_sim=sim, user_id=user_id
    )
    stats = await get_overall_stats(include_sim=sim, user_id=user_id)
    sparkline = [round(row["rate"], 3) for row in by_day]
    rate_change_pct_val = rate_change_pct(by_day)
    payload = {"by_day": by_day, "sparkline": sparkline, **stats}
    if rate_change_pct_val is not None:
        payload["rate_change_pct"] = rate_change_pct_val
    return payload


@app.get("/api/clusters")
async def get_clusters() -> list[dict[str, Any]]:
    docs = await list_clusters()
    return [_serialize(d) for d in docs]


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    return {
        "decision_interval_seconds": settings.decision_interval_seconds,
        "daily_surface_budget": settings.daily_surface_budget,
        "embedding_backend": settings.embedding_backend,
        "demo_mode": settings.demo_mode,
        "auto_heartbeat": settings.auto_heartbeat,
        "surface_score_threshold": settings.surface_score_threshold,
        "heartbeat_default_via_agent": settings.heartbeat_default_via_agent,
        "job_backend": settings.job_backend,
    }


@app.get("/api/context")
async def get_context(request: Request) -> dict[str, Any]:
    ctx = await get_context_async(get_user_id(request))
    payload = ctx.model_dump(mode="json")
    payload["_meta"] = context_meta(ctx)
    return payload


@app.get("/api/google/status")
async def google_status(request: Request) -> dict[str, Any]:
    """Whether the session user has stored Google tokens."""
    user_id = get_user_id(request) or settings.kairos_user_id
    if not user_id:
        return {
            "connected": False,
            "message": "Set KAIROS_USER_ID after connect_google (MCP or kairos google connect)",
        }
    record = await load_google_connection(user_id)
    if not record:
        return {"connected": False, "user_id": user_id}
    return {
        "connected": True,
        "user_id": user_id,
        "email": record.get("email"),
        "updated_at": record.get("updated_at").isoformat()
        if record.get("updated_at")
        else None,
    }


@app.post("/api/context/fuse")
async def post_fuse_context(body: FuseHeadspacePayload, request: Request) -> dict[str, Any]:
    user_id = get_user_id(request)
    saved = await fuse_and_persist_headspace(
        user_id=user_id,
        calendar_events=body.calendar_events or None,
        email_threads=body.email_threads or None,
        email_themes=body.email_themes,
        location_type=body.location_type,  # type: ignore[arg-type]
        lat=body.lat,
        lng=body.lng,
        surfaces_today=body.surfaces_today,
        time_since_last_surface_minutes=body.time_since_last_surface_minutes,
        sensor_sources=["web_fuse"],
    )
    payload = saved.model_dump(mode="json")
    payload["_meta"] = context_meta(saved)
    return payload


@app.post("/api/context")
async def post_set_context(body: SetContextRequest, request: Request) -> dict[str, Any]:
    user_id = get_user_id(request)
    base = await get_context_async(user_id)
    updates = body.model_dump(exclude_none=True)
    snapshot = base.model_copy(update=updates)
    if updates:
        sources = list(snapshot.sensor_sources)
        if "web" not in sources:
            sources.append("web")
        snapshot = enrich_modes(snapshot.model_copy(update={"sensor_sources": sources}))
    saved = await write_context(snapshot, user_id=user_id)
    return saved.model_dump(mode="json")


@app.post("/api/optimize")
async def post_optimize(
    dry_run: bool = Query(default=False),
    days: int = Query(default=14, ge=1, le=90),
    min_samples: int = Query(default=5, ge=1),
) -> dict[str, Any]:
    """Run one GEPA reflection pass over recent feedback."""
    from kairos.core.optimize import run_gepa

    return (await run_gepa(min_samples=min_samples, days=days, dry_run=dry_run)).model_dump(
        mode="json"
    )


@app.get("/api/optimize/runs")
async def get_optimize_runs(limit: int = Query(default=10, ge=1, le=50)) -> list[dict[str, Any]]:
    """Return recent GEPA optimization runs for the admin panel."""
    return await list_optimization_runs(limit=limit)


@app.get("/api/optimize/prompt")
async def get_active_digest_prompt() -> dict[str, Any]:
    """Return the currently active digest prompt (GEPA winner or default)."""
    from kairos.llm.generation import _DEFAULT_DIGEST_PROMPT

    active = await get_active_prompt()
    return {
        "prompt": active or _DEFAULT_DIGEST_PROMPT,
        "is_optimized": active is not None,
    }


@app.post("/api/feedback")
async def post_feedback(body: FeedbackRequest, request: Request) -> dict[str, Any]:
    result = await heartbeat_service.record_feedback(
        body.notification_id,
        body.action,
        url=body.url,
        user_id=get_user_id(request),
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=404, detail=result.get("message", "feedback failed"))
    return result


@app.post("/api/prep/start")
async def post_prep_start(
    body: PrepJobParams,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Queue bookmark prep (enrich → research → embed → cluster) in the background."""
    from kairos.jobs.dispatch import dispatch_prep_job

    params = body.model_dump(mode="json")
    job = await create_prep_job(params=body)
    backend = await dispatch_prep_job(job.job_id, params, background_tasks=background_tasks)
    return PrepJobStartResponse(job_id=job.job_id, status="pending", backend=backend).model_dump(
        mode="json"
    )


@app.get("/api/prep/{job_id}")
async def get_prep_status(job_id: str) -> dict[str, Any]:
    job = await get_prep_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="prep job not found")
    return job.model_dump(mode="json")


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
