## Learned User Preferences

- Prefer conventional commits grouped thematically (e.g. `feat(core):`, `feat(delivery):`, `docs:`) rather than one large commit.
- Keep documentation changes in separate `docs:` commits from code scaffolding.
- Delivery and UI should stay channel-agnostic: policy core decides; adapters handle web inbox, MCP host transcript, and optional OS notify.
- MCP integrations must respect host-agent mechanisms (render in transcript, optional local dashboard) rather than assuming a single delivery surface. Google OAuth and headspace sync live in Kairos MCP tools (`connect_google`, `sync_google_headspace`), not separate Cursor Google MCP servers.
- Google access must be channel-agnostic; per-user OAuth via loopback callback (MCP or CLI), not coupled to the web app.
- Prefer leveraging the intelligence layer (moment narrative, moment-fit, multi-step digest) even when it adds a few seconds of latency, if quality improves.
- Use one fixed embedding vector space in config; agents orchestrate pipeline timing, not per-request embedding model selection.
- Prefer Justfile (`just`) over ad-hoc shell scripts for demo and developer task orchestration.
- Digest link cards should show researched summaries (title, excerpt, tags) so users can judge bookmark value without opening x.com.

## Learned Workspace Facts

- Kairos is a contextual bandit for bookmark surfacing — optimizing when to interrupt, not on-demand search.
- LLM stack: Google ADK 2.0 (`google-adk`) for agent harness, Workspace MCP toolsets, and tool loop (Antigravity fully replaced); Gemini Interactions API (`google-genai`) for batch structured calls; embed API for vectors.
- Primary bookmark ingest is X API v2 `GET /2/users/{id}/bookmarks` (OAuth2 user token); X data export is the bootstrap fallback.
- Default embeddings via Gemini API (`gemini-embedding-001@768`); local sentence-transformers optional (`uv sync --extra local`, `EMBEDDING_BACKEND=local`). HDBSCAN clustering on embeddings; Gemini names and summarizes clusters after grouping.
- libSQL native vector search (`vector_distance_cos`) for cluster/bookmark ranking when available; falls back to in-memory cosine. See `docs/CLOUD_RUN.md` for deploy.
- Bandit params are per-user (`user_id × cluster_id × context_class`); sim gym uses `sim:{persona}` keys. Snooze is per-user within context class.
- `get_relevant_bookmarks` runs semantic search over the bookmark index; not exposed on the interrupt thesis path.
- `HeartbeatService` is the channel-agnostic policy core; reads fused headspace from `context_cache[user_id]` (no sensor fetch). Every heartbeat runs `prepare_context_for_decision` for moment narrative. Returns `KAIROS_OK` or `SURFACE` and fans out via delivery adapters (`web`, `os`, MCP return payload). Stage HTTP (`POST /api/heartbeat`, `/api/demo/surface`) calls `run_decision_cycle` → policy core directly, not the full ADK agent tool loop.
- Headspace sensors: hybrid path — ADK agent uses Workspace MCP + `fuse_headspace_context` (LLM-enriched); CLI/cron use `sync_google_headspace`.
- Intelligence layer (`llm/compose.py`, `core/intelligence.py`): LLM headspace fusion, moment-fit check, multi-step digest. Bandit + hard gates stay deterministic. Cluster digest uses Google Search grounding (`DIGEST_USE_GOOGLE_SEARCH`, default on). Batch `bookmarks research` runs `link_fetch` (HTTP preview) before Gemini grounded research; results persist on bookmark docs for digest link cards.
- Python package lives under `src/kairos`, packaged with hatch; dependencies managed with `uv`. Data store is libSQL/Turso — a local `kairos.db` file by default, with optional embedded-replica sync to a hosted Turso primary (`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`); tables: bookmarks, clusters, notifications, feedback_events, etc.; vector search drives ranking.
- Web gateway: FastAPI + SSE (`/api/stream`, `POST /heartbeat`) with an inbox UI; OpenClaw-style heartbeat ack for silence. Demo server: `just demo-serve` (`kairos bookmarks prep` / `just demo-corpus` = enrich + research + embed + cluster). Onboarding: `docs/DEVELOPER_GUIDE.md`, `/walkthrough`.
