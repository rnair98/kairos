# Kairos — tech debt & simplification plan

Prioritized **replace vs keep** decisions from the architecture review. Status tracked as we land changes.

Legend: ✅ done · 🚧 in progress · ⏳ planned · 🔒 keep (intentional custom)

---

## P0 — High ROI

| Item | Status | Action |
|------|--------|--------|
| Link extraction | ✅ | `trafilatura` in `link_fetch.py` |
| Unified prep pipeline | ✅ | `kairos bookmarks prep` + `just demo-corpus` |
| Single enrich path | ✅ | X sync raw-only; enrich via prep |
| Runtime digest fast path | ✅ | `INTELLIGENCE_DIGEST_RUNTIME_FAST` |
| Stable cluster IDs | ✅ | Centroid reuse ≥ `CLUSTER_ID_REUSE_THRESHOLD` |

## P1 — Scale & robustness

| Item | Status | Action |
|------|--------|--------|
| User-scoped notifications | ✅ | `list_notifications(user_id=…)`; feedback ownership check |
| Shared event stream | ✅ | the `pipeline_events` table + TTL; SSE replays persisted log |
| Job queue for prep | ✅ | `POST /api/prep/start` + `GET /api/prep/{job_id}`; optional Arq worker |
| Incremental cluster assign | ✅ | Batch assign embedded bookmarks only (no global null) |
| Google path unification | ✅ | `fuse_and_persist_headspace` shared by sync, MCP, web |

## P2 — Defer / keep custom

| Item | Status | Rationale |
|------|--------|-----------|
| Beta Thompson bandit | 🔒 | Correct for sparse feedback + few clusters |
| HeartbeatService | 🔒 | Product core |
| HDBSCAN + libSQL vector search | 🔒 | Standard stack |
| FastMCP thin wrapper | 🔒 | Not reinventing MCP |
| ADK agent path | ✅ | Sensor fusion via MCP; `via_agent` on heartbeat API/CLI; not default |
| GEPA / DSPy | ✅ | Eval harness + `kairos optimize run|readiness|eval` |
| Redis / Arq | ✅ | Optional `JOB_BACKEND=arq`; default `local` (FastAPI background) |

---

## Commands

```bash
# Full data-plane prep (sync CLI)
uv run kairos bookmarks prep
uv run kairos bookmarks prep --sync

# Background prep (while dashboard runs)
curl -X POST http://127.0.0.1:8420/api/prep/start -H 'Content-Type: application/json' -d '{}'
curl http://127.0.0.1:8420/api/prep/<job_id>

# Demo
just demo-corpus
just demo-serve
```

---

## Config flags

| Env | Default | Effect |
|-----|---------|--------|
| `INTELLIGENCE_DIGEST_RUNTIME_FAST` | `false` | Single LLM digest call at surface |
| `CLUSTER_ID_REUSE_THRESHOLD` | `0.88` | Keep cluster_id across recluster |
| `EVENT_PERSIST_ENABLED` | `true` | Write pipeline events to the database |
| `EVENT_PERSIST_TTL_DAYS` | `7` | TTL on `pipeline_events` collection |
| `JOB_BACKEND` | `local` | `local` = FastAPI background; `arq` = Redis worker |
| `REDIS_URL` | `redis://127.0.0.1:6379` | Arq broker when `JOB_BACKEND=arq` |
| `HEARTBEAT_DEFAULT_VIA_AGENT` | `false` | Web `POST /api/heartbeat` uses ADK when true |
| `COHORT_PRIOR_ENABLED` | `true` | Cold-start bandit α/β from other users on same cluster |
| `COHORT_PRIOR_MIN_USERS` | `2` | Min distinct users before applying cohort prior |

See [LOCAL_QUEUE.md](./LOCAL_QUEUE.md) for multi-process prep.

---

## Data models (P2)

Typed shapes under `src/kairos/models/`:

| Module | Models | Use |
|--------|--------|-----|
| `schemas.py` | `ContextSnapshot`, `ClusterDigest`, `DigestLinkCard`, `HeartbeatResult` | Policy core, API |
| `jobs.py` | `PrepJobParams`, `PrepJobRecord`, `PrepJobResult` | Prep API + database |
| `optimize.py` | `GepaReadiness`, `GepaRunResult`, `FixtureEvalResult` | GEPA CLI |
| `sensors.py` | `CalendarEvent`, `EmailThread`, `FuseHeadspacePayload` | MCP / web fuse |

**Still `dict` (future):** raw MCP calendar/email in `FuseHeadspaceRequest`, database bandit rows. Pipeline stages use `@dataclass` internally — fine.

---

## Delivered after P0–P2

These were previously P3 candidates and are now implemented.

| Priority | Item | Status | Notes |
|----------|------|--------|-------|
| **High** | Typed MCP fuse payloads | ✅ | `models/sensors.py` — `CalendarEvent`, `EmailThread`, `FuseHeadspacePayload` |
| **High** | Full multi-user web | ✅ | Session-scoped `/api/metrics`; cohort bandit priors (`COHORT_PRIOR_*`) |
| **Medium** | Incremental X sync CLI | ✅ | Early-stop when page all known; `--full-sync` / `--full`; default incremental on `prep --sync` |
| **Medium** | GEPA automation | ✅ | `kairos optimize nightly` + `just optimize-nightly` |

## What's next (P4)

Prioritized after the current demo-ready stack. See also [PLAN.md](../PLAN.md) stretch goals.

| Priority | Item | Notes |
|----------|------|-------|
| **High** | Latest learning trace panel | Compact dashboard proof: context → rank → gate → feedback → β update from `pipeline_events` |
| **High** | Treatment lift visualization | Show `bandit_treatments` so GEPA prompt changes become measurable treatments |
| **Medium** | Exact LLM trace join | Add `decision_id`, prompt version, input/output, latency, reward |
| **Medium** | Redis SSE fan-out | the `pipeline_events` table works cross-process; optional Redis pub/sub for lower latency |
| **Low** | OS delivery adapter | `terminal-notifier` path exists but optional |
| **Low** | Sleep-time consolidation | Pre-compute headspace narratives / cluster dossiers off-peak |
| **Low** | Multi-source ingest | Pocket, Readwise export — not X-only |

**Serialization:** JSON via `orjson` (stdlib `json` removed from app code).

## Related

- [DEVELOPER_GUIDE.md](./DEVELOPER_GUIDE.md)
- [ARCHITECTURE.md](./ARCHITECTURE.md)
