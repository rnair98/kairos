# Kairos Architecture

Kairos is a **contextual bandit** that learns **when** to surface bookmark *clusters* — not a search engine, not a cron digest. Silence (`KAIROS_OK`) is the default; interrupt only when calendar capacity, topical fit, and learned engagement align.

This document maps the running system in `src/kairos/`. For product thesis and roadmap, see [PLAN.md](../PLAN.md).

---

## System overview

```mermaid
flowchart TB
    subgraph Entry["Entry points"]
        CLI["CLI<br/><code>kairos</code>"]
        WebUI["Web dashboard<br/><code>kairos serve</code>"]
        Agent["ADK agent<br/><code>agent-cycle</code>"]
    end

    subgraph Policy["Policy plane"]
        Intel["Intelligence layer<br/><code>core/intelligence.py</code>"]
        HS["HeartbeatService"]
        Rank["Ranking pipeline"]
        Ctx["Context sensor"]
    end

    subgraph Data["Data plane"]
        Ingest["Ingest + enrich"]
        Embed["Embed + cluster"]
        Db[(Turso/libSQL)]
    end

    subgraph External["External services"]
        XAPI["X API v2"]
        Gemini["Gemini API"]
    end

    subgraph Output["Delivery + observability"]
        Del["Delivery adapters"]
        Bus["EventBus"]
        SSE["SSE /api/stream"]
    end

    CLI --> HS
    WebUI --> HS
    Agent --> HS

    HS --> Intel
    Intel --> Ctx
    HS --> Rank
    HS --> Del
    HS --> Db

    Rank --> Embed
    Rank --> Gemini
    Intel --> Gemini
    Ctx -->|"sync / fuse"| Calendar["Google Calendar + Gmail"]

    Ingest --> XAPI
    Ingest --> Gemini
    Ingest --> Db
    Embed --> Gemini
    Embed --> Db

    Del --> Bus
    Bus --> SSE
    SSE --> WebUI

    WebUI -->|"POST /api/feedback"| HS
```

---

## Layer model

| Layer | Responsibility | Key modules |
|-------|----------------|-------------|
| **Ingest** | Pull X bookmarks, normalize, enrich | `ingest/`, `bookmarks/enrich.py` |
| **Index** | Embed, cluster, fingerprint stale rows | `embeddings/`, `bookmarks/index.py` |
| **Context** | Headspace vector at decision time | `core/context.py`, `core/headspace.py`, `core/moment.py` |
| **Intelligence** | Gemini enrichment before policy | `core/intelligence.py`, `llm/compose.py`, `llm/generation.py` |
| **Policy** | Rank, gate, surface or silence | `core/ranking.py`, `core/bandit.py` |
| **Delivery** | Fan-out to web, OS, host transcript | `delivery/` |
| **Feedback** | Implicit signals → online bandit | `core/feedback.py`, `db/feedback.py` |
| **Observability** | Live activity stream | `observability/bus.py`, `web/app.py` |

---

## 1. Ingest layer

Pulls bookmarks from X, normalizes API payloads, and upserts into Turso/libSQL. **Enrichment is off during sync by default** — run `kairos bookmarks prep` or `kairos bookmarks enrich` separately.

```mermaid
flowchart LR
    subgraph X["X API v2"]
        OAuth["OAuth 2.0 PKCE<br/><code>ingest/x/oauth.py</code>"]
        Client["XApiClient<br/><code>ingest/x/client.py</code>"]
        Norm["normalize_bookmark<br/><code>ingest/x/normalize.py</code>"]
    end

    subgraph Sync["Sync + prep"]
        SyncFn["sync_bookmarks_from_x<br/>(raw upsert)"]
        Prep["kairos bookmarks prep<br/><code>bookmarks/pipeline.py</code>"]
        EnrichAPI["enrich + research<br/><code>bookmarks/enrich.py</code>"]
    end

    subgraph LLM["Gemini"]
        Gen["generation.py"]
    end

    subgraph Store["Turso/libSQL"]
        BM[("bookmarks")]
    end

    OAuth --> Client
    Client -->|"paginated GET /bookmarks"| Norm
    Norm --> SyncFn
    SyncFn -->|"upsert by x_tweet_id"| BM
    Prep --> EnrichAPI
    EnrichAPI --> Gen
    Gen --> BM
```

**Enrichment output** (`BookmarkEnrichment`): topic tags, consumption mode, energy cost, geo anchor, perishability.

**CLI:** `kairos x auth`, `kairos x sync`, `kairos bookmarks prep` (preferred), `kairos bookmarks enrich`

---

## 2. Embed + cluster (index layer)

One fixed vector space (config-driven). Fingerprints skip unchanged rows. HDBSCAN groups bookmarks into topic clusters.

```mermaid
flowchart TB
    subgraph Input
        Docs[("bookmarks<br/>without embedding")]
    end

    subgraph Encoder["Embedding encoder<br/><code>embeddings/encoder.py</code>"]
        Dispatch{backend?}
        Local["local_encoder<br/>BAAI/bge-small-en-v1.5"]
        GeminiEmb["gemini_encoder<br/>gemini-embedding-001@768"]
    end

    subgraph Cluster["Clustering<br/><code>bookmarks/index.py</code>"]
        FP["embed_fingerprint<br/><code>bookmarks/fingerprints.py</code>"]
        HDB["HDBSCAN"]
        Centroid["centroid_embedding"]
        Label["tag-heuristic name + summary"]
    end

    subgraph Output
        BM2[("bookmarks<br/>+ embedding + cluster_id")]
        CL[("clusters")]
    end

    Docs --> FP
    FP -->|"stale?"| Dispatch
    Dispatch -->|local| Local
    Dispatch -->|gemini default| GeminiEmb
    Local --> BM2
    GeminiEmb --> BM2
    BM2 --> HDB
    HDB --> Centroid
    Centroid --> Label
    Label --> CL
    CL -->|"member_count, centroid"| BM2
```

**Incremental pipeline** (`bookmarks/pipeline.py`): optional X sync → enrich → research → embed → cluster. Skips re-cluster when no new embeddings and no unclustered rows. Stable cluster IDs when centroid reuse ≥ `CLUSTER_ID_REUSE_THRESHOLD`.

**Background prep:** `POST /api/prep/start` → `dispatch_prep_job` (FastAPI background or Arq worker). Status in the `prep_jobs` table.

**CLI:** `kairos bookmarks prep`, `kairos bookmarks embed`, `kairos bookmarks cluster`, `kairos bookmarks clusters`

---

## 3. Context sensor + intelligence layer

Two dimensions drive the policy: **topical affinity** (what you're oriented toward) and **attention capacity** (whether interrupt is feasible). Raw sensors are fused heuristically, then **Gemini enriches** the snapshot before ranking.

```mermaid
flowchart TB
    subgraph Sources["Signal sources"]
        Cal["Google Calendar API / MCP"]
        Gmail["Gmail API / MCP"]
        Loc["Location / geofence"]
        Demo["Demo persona stub"]
    end

    subgraph Fuse["Heuristic fuse<br/><code>core/headspace.py</code>"]
        Parse["parse_calendar_events<br/>email theme extraction"]
    end

    subgraph Intel["Intelligence layer<br/><code>llm/compose.py</code>"]
        Enrich["enrich_headspace_from_sensors<br/>enrich_context_narrative"]
        Narrative["moment_narrative"]
    end

    subgraph Snapshot["ContextSnapshot"]
        Topical["topical_affinity · email_themes"]
        Capacity["attention_capacity · calendar_gap"]
        Moment["moment_narrative → ranking query"]
    end

    Cal --> Fuse
    Gmail --> Fuse
    Loc --> Fuse
    Demo --> Fuse
    Fuse --> Intel
    Intel --> Snapshot
```

| Field | Role |
|-------|------|
| `calendar_gap_minutes` | Hard gate: min gap before interrupt |
| `moment_narrative` | LLM-composed query text for vector match (replaces template `moment_text`) |
| `topical_affinity` / `attention_capacity` | LLM-refined modes (heuristic fallback) |
| `surfaces_today` | Daily budget / fatigue |

**Sync paths:** `sync_google_headspace` and `fuse_headspace_context` call `fuse_headspace_intelligent` when `INTELLIGENCE_HEADSPACE_ENABLED=true`. Every heartbeat tick runs `prepare_context_for_decision` to compose `moment_narrative` if missing.

---

## 4. Ranking pipeline

The thesis lives here: enriched moment → cluster fit × learned bandit weight → hard gates → **LLM moment-fit** → multi-step digest or silence.

```mermaid
flowchart TB
    Start([evaluate_surface]) --> Snooze["Filter snoozed clusters"]
    Snooze --> EmbedQ["encode_query(moment_narrative)<br/><code>core/moment.py</code>"]

    EmbedQ --> Loop{{"For each cluster"}}
    Loop --> Cosine["cosine(moment_vec, centroid)"]
    Cosine --> TS["Thompson sample α,β"]
    TS --> Adj["adjusted = vector × bandit_weight"]
    Adj --> Best["Pick best cluster"]

    Best --> Gate{"Hard interrupt gate"}
    Gate --> G1["daily_budget"]
    Gate --> G2["calendar_gap"]
    Gate --> G3["min_gap since last surface"]
    Gate --> G4["score_threshold"]

    G1 & G2 & G3 & G4 --> HardOK{all pass?}
    HardOK -->|no| OK["KAIROS_OK"]
    HardOK -->|yes| Fit["check_moment_fit<br/><code>llm/compose.py</code>"]
    Fit --> FitOK{fit?}
    FitOK -->|no| OK
    FitOK -->|yes| Digest["Multi-step digest<br/>draft → search → critique → revise"]
    Digest --> Links["Merge real bookmark URLs"]
    Links --> Surface["SURFACE + ClusterDigest"]

    OK --> Emit["EventBus"]
    Surface --> Emit
```

```mermaid
sequenceDiagram
    participant HS as HeartbeatService
    participant Intel as prepare_context_for_decision
    participant Rank as evaluate_surface
    participant Emb as encode_query
    participant Band as bandit_params
    participant Fit as check_moment_fit
    participant LLM as generate_cluster_digest

    HS->>Intel: ContextSnapshot
    Intel-->>HS: + moment_narrative
    HS->>Rank: enriched context
    Rank->>Emb: moment_text → narrative
    Emb-->>Rank: moment vector
    loop each cluster
        Rank->>Band: get_bandit_params
        Rank->>Rank: adjusted = cosine × Thompson(α,β)
    end
    alt hard gates pass
        Rank->>Fit: cluster + snippets + context
        Fit-->>Rank: fit / reason
        alt moment fit
            Rank->>LLM: draft → search → critique → revise
            LLM-->>Rank: ClusterDigest
            Rank-->>HS: should_surface=true
        else moment misfit
            Rank-->>HS: should_surface=false
        end
    else gates fail
        Rank-->>HS: should_surface=false
    end
```

**Module map:** `core/ranking.py` · `core/bandit.py` · `core/moment.py` · `llm/compose.py` · `llm/generation.py` · `db/bandit.py`

**Policy vs intelligence:** bandit + hard gates stay deterministic. Gemini adds narrative enrichment (every tick), moment-fit check (surface path only), and digest quality (surface path only).

**Performance:** budget/gap gates run before vector encode + bandit batch fetch; moment-fit and digest only run when hard gates + score threshold pass. Cluster and bookmark ranking use libSQL vector search when indexes exist, with in-memory cosine fallback. Evergreen clusters skip Google Search grounding during digest (`digest_skip_search_evergreen`). Snooze is scoped per **user × context_class**.

---

## 5. HeartbeatService (policy core)

Single orchestrator for every runtime path — CLI, web, agent, MCP.

```mermaid
stateDiagram-v2
    [*] --> ReadContext: run()
    ReadContext --> Enrich: prepare_context_for_decision()
    Enrich --> Evaluate: evaluate_surface()
    Evaluate --> KairosOK: not should_surface
    Evaluate --> Persist: should_surface
    Persist --> Deliver: save_notification()
    Deliver --> Surface: deliver(adapters)
    KairosOK --> [*]: HeartbeatResult KAIROS_OK
    Surface --> [*]: HeartbeatResult SURFACE

    note right of KairosOK
        event_bus: indicator KAIROS_OK
        reason from gate_reasons
    end note

    note right of Surface
        event_bus: indicator SURFACE
        notification UUID persisted
    end note
```

```mermaid
flowchart LR
    subgraph HeartbeatService["core/heartbeat.py"]
        Run["run()"]
        FB["record_feedback()"]
    end

    subgraph Deps
        Ctx["read_context"]
        Intel["prepare_context_for_decision"]
        Rank["evaluate_surface"]
        Save["save_notification"]
        Del["deliver"]
        Proc["process_feedback"]
    end

    Run --> Ctx --> Intel --> Rank
    Rank -->|SURFACE| Save --> Del
    FB --> Proc
```

**Contract:** `HeartbeatResult` — same shape for HTTP, CLI JSON, MCP, and ADK agent.

All entry points (web, CLI, MCP `run_heartbeat`, ADK `agent-cycle`) call `HeartbeatService`, which runs the intelligence layer before policy. ADK orchestrates sensors + tools; policy + intelligence stay in the Python core.

---

## 6. Delivery layer

Adapters fan out surfaced digests without changing policy logic.

```mermaid
flowchart TB
    HS["HeartbeatService"]
    Reg["delivery/registry.py"]

    subgraph Adapters
        Web["WebDeliveryAdapter<br/><code>delivery/web.py</code>"]
        OS["OSDeliveryAdapter<br/><code>delivery/os.py</code>"]
        Ret["return_only<br/>(no adapter)"]
    end

    subgraph Consumers
        Bus["EventBus"]
        SSE["FastAPI SSE"]
        Inbox["index.html inbox"]
        Term["terminal-notifier<br/>🚧 optional"]
        Host["MCP host transcript"]
    end

    HS --> Reg
    Reg --> Web
    Reg --> OS
    Reg --> Ret

    Web -->|"emit notification"| Bus
    Bus --> SSE --> Inbox
    OS -.-> Term
    Ret --> Host
```

**Render:** `delivery/render.py` — markdown digest + delivery hints for host agents.

---

## 7. Feedback loop + contextual bandit

Online learning without LLM fine-tuning. Snooze ≠ dismiss. Bandit params are keyed by **user × cluster × context_class** (`user_id` from session or `KAIROS_USER_ID`).

```mermaid
flowchart TB
    subgraph UI["User actions"]
        Snooze["Snooze 2h"]
        Dismiss["Not relevant"]
        Click["Link click"]
    end

    subgraph API
        POST["POST /api/feedback<br/>kairos feedback"]
    end

    subgraph Process["process_feedback<br/><code>core/feedback.py</code>"]
        Lookup["get_notification"]
        Reward["reward_for_action<br/><code>core/rewards.py</code>"]
        Insert["insert_feedback_event"]
        Update["apply_bandit_reward"]
        Status["update_notification_status"]
    end

    subgraph Db[(Turso/libSQL)]
        FE[("feedback_events")]
        BP[("bandit_params")]
        NT[("notifications")]
    end

    Snooze --> POST
    Dismiss --> POST
    Click --> POST

    POST --> Lookup --> Reward
    Reward --> Insert --> FE
    Reward -->|"reward ≠ null"| Update --> BP
    Reward -->|"snooze: null reward"| Status
    Update --> Status --> NT
```

**Reward table**

| Action | Reward | Bandit update |
|--------|--------|---------------|
| `acted` | +1.0 | α += 1.0 |
| `link_click` | +0.8 | α += 0.8 |
| `expanded` | +0.4 | α += 0.4 |
| `snoozed` | — | Exclude cluster from ranking (TTL) |
| `dismissed` | −0.4 | β += 0.4 |
| `ignored` | −0.6 | β += 0.6 |

```mermaid
flowchart LR
    subgraph Bandit["Thompson sampling<br/>per cluster × context_class"]
        Alpha["α successes"]
        Beta["β failures"]
        Sample["weight ~ Beta(α,β)"]
    end

    Alpha --> Sample
    Beta --> Sample
    Sample --> Rank["adjusted score"]
    Rank --> Next["next heartbeat"]
    Feedback["feedback_events"] --> Alpha
    Feedback --> Beta
```

---

## 8. Observability + web gateway

In-process pub/sub streams agent activity to the dashboard admin panel. When `EVENT_PERSIST_ENABLED=true`, events are also written to the `pipeline_events` table (TTL) so CLI/MCP heartbeats appear in the admin log after browser refresh.

```mermaid
flowchart TB
    subgraph Producers["EventBus emitters"]
        HS["HeartbeatService"]
        Rank["ranking.py"]
        Ctx["context.py"]
        Del["WebDeliveryAdapter"]
        FB["record_feedback"]
        Intel["llm/compose.py"]
        Agent["agent/agent.py callbacks"]
    end

    subgraph Bus["EventBus<br/><code>observability/bus.py</code>"]
        Hist["history buffer (500)"]
        Sub["asyncio.Queue subscribers"]
    end

    subgraph Web["FastAPI<br/><code>web/app.py</code>"]
        Stream["GET /api/stream SSE"]
        Notif["GET /api/notifications"]
        BanditAPI["GET /api/bandit"]
        MetricsAPI["GET /api/metrics"]
        OptimizeAPI["POST /api/optimize"]
        PrepAPI["POST /api/prep/start"]
        HeartbeatAPI["POST /api/heartbeat"]
        FeedbackAPI["POST /api/feedback"]
    end

    subgraph UI["static/index.html"]
        User["User inbox"]
        Admin["Admin activity feed"]
    end

    Producers --> Bus
    Bus --> Stream
    Stream --> Admin
    Notif --> User
    BanditAPI --> Admin
    MetricsAPI --> Admin
    OptimizeAPI --> Admin
    PrepAPI --> Admin
    HeartbeatAPI --> HS
    FeedbackAPI --> FB
```

**Event kinds:** `session`, `context`, `intelligence`, `activity`, `indicator`, `notification`, `feedback`, `turn`, `tool_call`, `cluster`, `search`

---

## 9. ADK agent + MCP

**ADK agent** (`kairos agent-cycle` or `heartbeat --via-agent`) fetches Calendar/Gmail via **Workspace MCP**, calls `fuse_headspace_context`, then `run_heartbeat`. **Kairos MCP** (`kairos mcp`) exposes policy tools directly — use `sync_google_headspace` for Calendar/Gmail fetch + fuse. Both call the same `HeartbeatService`.

```mermaid
flowchart TB
    subgraph Host["MCP host (Claude Code)"]
        Loop["/loop 5m"]
    end

    subgraph ADK["ADK agent path"]
        AG["LlmAgent<br/><code>agent/agent.py</code>"]
        CalMCP["Calendar MCP"]
        GmailMCP["Gmail MCP"]
    end

    subgraph KairosMCP["Kairos MCP tools"]
        RH["run_heartbeat"]
        RF["record_feedback"]
        Fuse["fuse_headspace_context"]
    end

    subgraph Core["Shared core"]
        Intel["Intelligence layer"]
        HS["HeartbeatService"]
    end

    Loop --> AG
    AG --> CalMCP
    AG --> GmailMCP
    AG --> Fuse
    AG --> RH
    Loop --> RH
    RH --> Intel --> HS
    RF --> HS
```

Direct CLI and web paths skip ADK orchestration but run the same intelligence + policy:

```mermaid
flowchart TB
    subgraph Direct["Direct path"]
        CLI1["kairos heartbeat"]
        API["POST /api/heartbeat"]
        MCP["kairos mcp → run_heartbeat"]
    end

    subgraph AgentPath["ADK path"]
        CLI2["kairos agent-cycle"]
        AG["LlmAgent + Workspace MCP"]
    end

    subgraph Core["Shared core"]
        Intel["prepare_context_for_decision"]
        HS["HeartbeatService"]
    end

    CLI1 --> HS
    API --> HS
    MCP --> HS
    CLI2 --> AG --> RH["run_heartbeat"] --> HS
    HS --> Intel
```

| Tool (MCP + harness) | Status | Purpose |
|------|--------|---------|
| `run_heartbeat` | ✅ | Policy cycle |
| `record_feedback` | ✅ | Bandit update |
| `get_current_context` | ✅ | Cached/demo headspace snapshot |
| `get_cluster_summary` | ✅ | Topic → cluster lookup |
| `get_relevant_bookmarks` | ✅ | Semantic search over bookmark index (not thesis) |
| `add_bookmark` | — | Not exposed; use `kairos x sync` |

---

## 10. CLI surface

```mermaid
mindmap
  root((kairos))
    Policy
      heartbeat
      heartbeat --via-agent
      feedback
      agent-cycle
      optimize run|readiness|eval|nightly
    Web
      serve
      mcp
      worker
    Bookmarks
      prep
      list
      enrich
      research
      embed
      cluster
      clusters
    Ingest
      x auth
      x sync
      x refresh
    Dev
      x whoami
      x auth-check
```

---

## 11. Database tables

```mermaid
erDiagram
    bookmarks ||--o| clusters : "cluster_id"
    notifications }o--|| clusters : "cluster_id"
    feedback_events }o--|| notifications : "notification_id"
    feedback_events }o--|| clusters : "cluster_id"
    bandit_params }o--|| clusters : "cluster_id"
    bandit_treatments }o--|| clusters : "cluster_id"

    bookmarks {
        string x_tweet_id UK
        string url
        string raw_text
        float[] embedding
        string cluster_id
        list topic_tags
        string consumption_mode
        float energy_cost
        string embed_fingerprint
    }

    clusters {
        string cluster_id UK
        string name
        string summary
        float[] centroid_embedding
        int member_count
    }

    notifications {
        string notification_id UK
        string cluster_id
        object digest
        object context_snapshot
        string status
        datetime created_at
    }

    feedback_events {
        string event_id UK
        string notification_id
        string cluster_id
        string context_class
        string action
        float derived_reward
        object context_snapshot
    }

    bandit_params {
        string user_id
        string cluster_id
        string context_class
        float alpha
        float beta
        datetime last_updated
    }

    bandit_treatments {
        string user_id
        string cluster_id
        string context_class
        string digest_style
        float alpha
        float beta
        datetime last_updated
    }

    prep_jobs {
        string job_id UK
        string status
        object params
        object result
    }

    pipeline_events {
        datetime timestamp
        string kind
        string message
        object data
    }

    optimization_runs {
        datetime run_at
        string prompt_before
        string prompt_after
        float engagement_delta
    }
```

Collections also include `context_cache`, `google_tokens`, `oauth_states`, `sync_state`, and `optimization_runs` (GEPA prompt diffs).

---

## 12. LLM layer

Structured generation uses the Gemini Interactions API. The **intelligence layer** runs on every heartbeat tick and on the surface path.

```mermaid
flowchart LR
    subgraph Callers
        IngestEnrich["bookmark enrich"]
        Headspace["headspace compose"]
        MomentFit["moment fit check"]
        Digest["multi-step digest"]
    end

    subgraph LLM["llm/"]
        Client["client.py"]
        Compose["compose.py"]
        Gen["generation.py"]
        GroundMod["grounding.py"]
    end

    subgraph Models
        Lite["gemini-3.1-flash-lite<br/>headspace · fit · critique"]
        Flash["gemini-3.5-flash<br/>digest draft · revise"]
        EmbedM["gemini-embedding-001"]
    end

    IngestEnrich --> Gen --> Lite
    Headspace --> Compose --> Lite
    MomentFit --> Compose --> Lite
    Digest --> Gen --> Flash
    Digest --> Compose --> Lite
    Gen --> GroundMod
    EmbedM -.->|"embeddings/"| EmbedEnc["gemini_encoder.py"]
```

**Digest pipeline:** structured draft → optional Google Search grounding → LLM critique → revise if weak (`INTELLIGENCE_DIGEST_MULTISTEP`).

**Env flags:** `INTELLIGENCE_HEADSPACE_ENABLED`, `INTELLIGENCE_MOMENT_FIT_CHECK`, `INTELLIGENCE_DIGEST_MULTISTEP`, `DIGEST_USE_GOOGLE_SEARCH`.

---

## 13. Self-improvement

```mermaid
flowchart TB
    subgraph Online["Online — shipped"]
        FB["feedback_events"]
        BP["bandit_params"]
        BT["bandit_treatments"]
        TS["Thompson sampling"]
    end

    subgraph Offline["Offline — shipped (manual / cron-safe trigger)"]
        Eval["Eval harness<br/>fixed context×cluster fixtures"]
        GEPA["GEPA reflection pass<br/>kairos optimize run|nightly / POST /api/optimize"]
        OR["optimization_runs<br/>prompt diffs"]
    end

    subgraph Dashboard
        Chart["Engagement curve"]
        Diff["Prompt diff panel"]
    end

    FB --> Eval
    FB --> BP
    FB --> BT
    Eval --> GEPA --> OR
    OR --> Diff
    BP --> Chart
    BT --> Chart
    TS --> Online
```

**Automation:** `kairos optimize nightly` / `just optimize-nightly` is cron-safe and skips when `gepa_ready=false`. Cloud Run Scheduler is deployment wiring, not application logic.

---

## 14. End-to-end lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant X as X API
    participant Ingest
    participant Db
    participant Heartbeat
    participant Rank
    participant Web
    participant Bandit

    Note over X,Db: Data plane (batch / incremental)
    User->>Ingest: kairos bookmarks prep --sync
    Ingest->>X: GET bookmarks
    X-->>Ingest: tweets
    Ingest->>Db: bookmarks + enrichment + research + embeddings + clusters

    Note over User,Bandit: Policy plane (heartbeat loop)
    User->>Heartbeat: kairos heartbeat / POST /api/heartbeat
    Heartbeat->>Heartbeat: prepare_context_for_decision
    Heartbeat->>Rank: evaluate_surface
    Rank->>Db: clusters, bandit_params
    Rank-->>Heartbeat: SURFACE + digest
    Heartbeat->>Db: save notification
    Heartbeat->>Web: EventBus → SSE → inbox

    User->>Web: Dismiss / Snooze
    Web->>Heartbeat: POST /api/feedback
    Heartbeat->>Db: feedback_events + bandit_params + bandit_treatments
    Heartbeat->>Web: SSE feedback event

    User->>Heartbeat: next heartbeat
    Heartbeat->>Rank: evaluate_surface
    Rank->>Bandit: updated α/β
    Rank-->>Heartbeat: KAIROS_OK or different cluster
```

---

## 15. Configuration

Central settings in `config.py` (env + `.env`):

| Setting | Default | Effect |
|---------|---------|--------|
| `EMBEDDING_BACKEND` | `gemini` | Vector encoder dispatch |
| `DAILY_SURFACE_BUDGET` | `3` | Max surfaces / day |
| `SURFACE_SCORE_THRESHOLD` | `0.12` | Min adjusted score |
| `MIN_CALENDAR_GAP_MINUTES` | `30` | Attention capacity gate |
| `SNOOZE_TTL_MINUTES` | `120` | Snooze exclusion window |
| `DIGEST_USE_GOOGLE_SEARCH` | `true` | Ground digest with web |
| `INTELLIGENCE_HEADSPACE_ENABLED` | `true` | LLM headspace + moment narrative |
| `INTELLIGENCE_MOMENT_FIT_CHECK` | `true` | LLM gate before digest |
| `INTELLIGENCE_DIGEST_MULTISTEP` | `true` | Critique + revise digest (off when runtime fast) |
| `INTELLIGENCE_DIGEST_RUNTIME_FAST` | `false` | Single LLM digest call at surface (demo: `true`) |
| `CLUSTER_ID_REUSE_THRESHOLD` | `0.88` | Keep cluster_id when centroid matches |
| `EVENT_PERSIST_ENABLED` | `true` | Persist pipeline events to the database for SSE replay |
| `JOB_BACKEND` | `local` | `local` or `arq` for prep jobs |
| `HEARTBEAT_DEFAULT_VIA_AGENT` | `false` | Web heartbeat uses ADK when true |
| `GEPA_ENABLED` | `true` | Enable GEPA reflection pass |
| `GEPA_MIN_SAMPLES` | `5` | Feedback threshold for GEPA |
| `COHORT_PRIOR_ENABLED` | `true` | Cold-start α/β from other users on same cluster/context |
| `COHORT_PRIOR_MIN_USERS` | `2` | Min distinct users for cohort prior |
| `DELIVERY_TARGETS` | `web` | Adapter fan-out |

---

## Module index

```
src/kairos/
├── cli.py                 # CLI entry
├── config.py              # Settings
├── agent/                 # ADK agent + MCP tools
├── bookmarks/             # Enrich, research, embed, cluster, pipeline, prep jobs
├── jobs/                  # Arq worker + dispatch (optional queue)
├── core/                  # Policy + intelligence: context, ranking, bandit, optimize
│   ├── intelligence.py    # fuse_headspace_intelligent, prepare_context
│   └── eval_harness.py    # GEPA fixture eval
├── models/                # Pydantic: schemas, jobs, optimize
├── llm/                   # Gemini generation, compose, interactions
├── db/                    # Turso/libSQL repositories
├── delivery/              # Web + OS adapters
├── embeddings/            # Local + Gemini encoders
├── ingest/                # X OAuth, sync, normalize
├── observability/         # EventBus + db pipeline_events
├── web/                   # FastAPI + static dashboard
└── mcp/                   # FastMCP server (stdio)
```

---

## Related docs

- [PLAN.md](../PLAN.md) — product thesis and roadmap
- [TECH_DEBT.md](TECH_DEBT.md) — simplification roadmap + what's next
- [LOCAL_QUEUE.md](LOCAL_QUEUE.md) — optional Arq prep queue
- [archive/](archive/) — research notes
