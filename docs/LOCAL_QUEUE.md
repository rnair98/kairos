# Local job queue (Arq + Redis)

Prep jobs (`POST /api/prep/start`) run in-process by default via FastAPI `BackgroundTasks`. For multi-process setups — API server + separate worker — use **Arq** on Redis.

## Quick start

```bash
# 1. Install queue extras
uv sync --extra queue

# 2. Start Redis
docker compose up -d redis
# or: just redis-up

# 3. Terminal A — API with Arq dispatch
JOB_BACKEND=arq uv run kairos serve --port 8420

# 4. Terminal B — worker
uv run kairos worker
# or: just worker
```

Poll job status: `GET /api/prep/{job_id}` (Database-backed; same as local mode).

## Config

| Env | Default | Effect |
|-----|---------|--------|
| `JOB_BACKEND` | `local` | `local` or `arq` |
| `REDIS_URL` | `redis://127.0.0.1:6379` | Arq broker |

## Why Arq over Celery

Kairos is already async (Motor, FastAPI). Arq runs native async tasks on Redis with minimal boilerplate — no separate result backend, no sync worker pool.

## CLI

```bash
uv run kairos worker                    # start worker
uv run arq kairos.jobs.arq_settings.WorkerSettings   # equivalent
```

## When to stay on `local`

Single-process demos (`just demo-serve`) and local dev don't need Redis. the `prep_jobs` table tracks status either way.
