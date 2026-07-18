# Kairos — Plan

> **Status (2026-06):** P0-P2 simplification landed — see [docs/TECH_DEBT.md](docs/TECH_DEBT.md) for current state and P3 backlog. This doc retains the product thesis and roadmap.

> *kairos* (Greek): the right or opportune moment. The agent that turns a passive bookmark graveyard into execution by learning *when* to surface information, not just *what*.

## What We're Building

A context-aware agent that learns the optimal moment to surface Twitter/X bookmarks based on calendar state, location, time patterns, and headspace signals — with zero friction feedback and a nightly self-improvement pass. Passive hoarding → timely execution.

**The thesis in one sentence:** Everyone embeds the bookmark; nobody optimizes the interruption policy against measured attention outcomes and lets it rewrite itself.

---

## Core Insight: This Is Not a Search Problem

The naive version embeds bookmarks, does cosine similarity, and sends a push notification. That's a cron job with a vector index. It fails because:
- It fires at wrong moments and gets ignored
- It never learns from that ignoring
- Silence is never a feature

Kairos is a **contextual bandit**: at each candidate moment, score bookmark clusters for fit-to-this-moment, decide whether to interrupt at all, and update the policy on sparse implicit feedback. "Learns when depending on headspace" is the exact specification of a bandit policy improving over time.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        INGEST LAYER                         │
│  X API GET /2/users/{id}/bookmarks (paginated sync)         │
│  → normalize → LLM enrichment (Gemini flash-lite)           │
│  → Turso/libSQL + embeddings → HDBSCAN clustering            │
│  (fallback: X data export for bootstrap without OAuth)      │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                      CONTEXT SENSOR                         │
│  Google Workspace MCP (list_events) · Location toggle       │
│  Headspace = topical affinity vector + attention capacity   │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                     RANKING PIPELINE                        │
│  1. Feasibility filter (energy cost, restraint budget)      │
│  2. Topical score ($vectorSearch: moment → cluster)         │
│  3. Bandit adjustment (Thompson sampling, learned weights)  │
│  4. Interrupt gate (threshold check → surface or silence)   │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│              HeartbeatService (policy core)                 │
│  read_context → evaluate_surface → save_notification        │
│  → deliver (adapter fan-out) → HeartbeatResult              │
└──────────────────────────┬──────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
  WebDeliveryAdapter  OSDeliveryAdapter  HeartbeatResult
  (→ EventBus SSE)   (terminal-notifier) (MCP/Antigravity
                                          host transcript)
        │
        ▼
  EventBus (in-process pub/sub)
  → FastAPI SSE → web dashboard
        │
        └──────────────────────────────────────┐
                                               ▼
                                   ┌─────────────────────┐
                                   │  SELF-IMPROVEMENT   │
                                   │  BANDIT (online)    │
                                   │  GEPA PASS (nightly)│
                                   └─────────────────────┘
```

---

## Runtime Paths

There are two ways to invoke a heartbeat cycle. Both go through the same `HeartbeatService` policy core.

**Direct path** (`kairos heartbeat`): calls `heartbeat_service.run()` directly. Fastest — used by dashboard, demo, and Kairos MCP `run_heartbeat`.

**ADK agent path** (`kairos agent-cycle` or `heartbeat --via-agent`): Google ADK agent fetches Calendar/Gmail via Workspace MCP, fuses headspace, then calls `run_heartbeat`. Slower; use when MCP sensor payloads must be fetched by the agent loop.

```
CLI / Claude Code /loop / FastMCP
         │
         ├── direct: heartbeat_service.run()
         │
         └── agent: Antigravity Agent → Gemini → tool calls → heartbeat_service
                          │
                          └── hooks: post_tool_call, post_turn → EventBus
```

---

## Notification Format: Cluster Digest, Not Single Bookmark

The unit surfaced is a **topic cluster digest**, not an individual bookmark:

```
┌──────────────────────────────────────────────────┐
│  Distributed Systems (8 bookmarks)               │
│                                                  │
│  You're heading into an infra architecture       │
│  meeting in 40 min. These might be useful:       │
│                                                  │
│  · [CAP theorem + modern tradeoffs] — dense read │
│  · [Kafka vs Redpanda thread] — 3 min skim       │
│  · [Jepsen test results for Postgres] — reference│
│                                                  │
│  [Open all]  [Snooze 2h]  [Not relevant]         │
└──────────────────────────────────────────────────┘
```

Links within a digest are ranked by relevance to current context. Snooze applies to the whole cluster for this context window and re-queues it with the context snapshot stamped on it.

---

## Headspace: Two Dimensions

**Topical affinity** — what are you mentally oriented toward?
- Upcoming calendar event titles (embedded as intent signal) — via Google Workspace MCP `list_events`
- Recent event titles (topic trail from what just ended)
- Location type: desk → work mode, cafe → exploratory, gym → nothing technical
- Post-meeting window: 15–30 min after a multi-person event, topics are primed

**Attention capacity** — how much cognitive bandwidth is available?
- Calendar gap size (minutes until next event)
- Meeting density today (% of day in meetings)
- Minutes since last meeting (recovery window)
- Surfaces already consumed today (fatigue proxy)

Topical affinity → which cluster to surface. Attention capacity → whether any cluster is feasible.

---

## Data Models

All models in `src/kairos/models/schemas.py`. Database tables (Turso/libSQL):

### `bookmarks`

```python
{
  "_id": ObjectId(),
  "x_tweet_id": str,                  # unique upsert key from X API
  "url": str,
  "raw_text": str,
  "author_id": str,
  "author_username": str,
  "tweet_created_at": datetime,
  "context_annotations": list[dict],  # X-inferred entities — seed for topic_tags
  "referenced_tweets": list[dict],    # quoted/replied-to context
  "embedding": list[float],           # 384-dim, sentence-transformers all-MiniLM-L6-v2
  "cluster_id": ObjectId,
  "topic_tags": list[str],            # from BookmarkEnrichment (Gemini flash-lite)
  "consumption_mode": str,            # read-deep | skim | watch | act-in-world | save-to-project
  "energy_cost": float,               # 0.0–1.0
  "geo_anchor": str | None,
  "geo_coords": [float, float] | None,
  "perishability": str,               # evergreen | dated | time-sensitive
  "ingested_at": datetime,
  "last_synced_at": datetime,
  "last_surfaced_at": datetime | None,
  "surface_count": int,
}
```

### `clusters`

```python
{
  "_id": ObjectId(),
  "name": str,                        # LLM-generated label
  "summary": str,                     # 2-sentence summary, GEPA-tuned
  "centroid_embedding": list[float],
  "member_count": int,
  "last_updated": datetime,
}
```

### `notifications`

```python
{
  "_id": ObjectId(),
  "notification_id": str,             # uuid, matches NotificationRecord
  "cluster_id": ObjectId,
  "digest": dict,                     # ClusterDigest payload
  "context_snapshot": dict,
  "status": str,                      # pending | snoozed | dismissed | acted | expired
  "created_at": datetime,
  "expires_at": datetime | None,
}
```

### `feedback_events`

```python
{
  "_id": ObjectId(),
  "notification_id": str,
  "cluster_id": ObjectId,
  "context_snapshot": dict,
  "notification_text": str,           # exact rendered markdown (GEPA eval input)
  "events": [                         # raw interaction sequence
    { "type": "shown",      "t": 0 },
    { "type": "expanded",   "t": 4 },
    { "type": "link_click", "t": 9,  "url": str },
    { "type": "dismissed",  "t": 61 },
  ],
  "derived_reward": float,
  "snooze_context": dict | None,
  "created_at": datetime,
}
```

### `bandit_params`

```python
{
  "cluster_class": str,
  "context_class": str,
  "alpha": float,                     # Thompson sampling beta distribution
  "beta": float,
  "last_updated": datetime,
}
```

### `optimization_runs`

```python
{
  "run_at": datetime,
  "prompt_before": str,
  "prompt_after": str,
  "engagement_before": float,
  "engagement_after": float,
  "diff_summary": str,                # "what I learned" — closing demo slide
}
```

---

## Ranking Pipeline (`core/ranking.py`)

### Step 1 — Feasibility Filter

SQL pre-filter before vector search:
```python
{ "energy_cost": { "$lte": available_capacity },
  "cluster_id": { "$nin": snoozed_cluster_ids } }
```

### Step 2 — Topical Score (libSQL `vector_distance_cos`)

```python
pipeline = [
  { "$vectorSearch": {
      "index": "bookmark_embedding_index",
      "path": "embedding",
      "queryVector": moment_vector,     # embedded headspace context
      "numCandidates": 50,
      "limit": 10,
  }},
  { "$match": { "energy_cost": { "$lte": available_capacity } } },
  { "$addFields": { "vector_score": { "$meta": "vectorSearchScore" } } },
  { "$sort": { "vector_score": -1 } }
]
```

### Step 3 — Bandit Adjustment

Thompson sample from `bandit_params` per cluster × context class. `adjusted_score = vector_score × bandit_weight`. Learned history reshapes pure similarity over time.

### Step 4 — Interrupt Gate

```
surfaces_today < daily_budget          ✓/✗
calendar_gap_minutes > energy_cost     ✓/✗
time_since_last_surface > min_gap      ✓/✗
adjusted_score > learned_threshold     ✓/✗

All pass → SurfaceDecision(should_surface=True)
Any fail → SurfaceDecision(should_surface=False)  ← silence is the feature
```

Gate reasons are included in `SurfaceDecision.gate_reasons` and emitted to the EventBus for the dashboard.

---

## Reward Function

| Action | Reward | Notes |
|--------|--------|-------|
| `acted` | +1.0 | Passive → execution achieved |
| `link_click` ×2+ | +0.8 | Strong engagement |
| `link_click` + dwell >30s | +0.6 | Solid engagement |
| `expanded` | +0.4 | Interest signal |
| `expanded` only, no click | +0.2 | Weak positive |
| `snoozed` | 0.0 (re-queue) | Right thing, wrong time — re-queue with context stamp |
| `dismissed` | −0.4 | Wrong cluster |
| `ignored` (expired) | −0.6 | Trained user to ignore |

Dwell alone is not a positive label — requires `expanded` or `link_click`. Guards against Goodhart: the agent cannot win by writing longer summaries.

---

## Two Self-Improvement Loops

```
feedback_event.derived_reward
        │
        ├──► BANDIT UPDATE (online, after every feedback event)   ✅ SHIPPED
        │    Updates: bandit_params alpha/beta for user × cluster × context
        │    Also updates bandit_treatments for digest_style (GAMBITTS-lite)
        │    Wired: core/feedback.py → db/bandit.apply_*_reward()
        │
        └──► GEPA OPTIMIZATION (offline, manual or nightly)        ⚠️ SHIPPED, UNGATED
             Trigger: kairos optimize run | kairos optimize nightly | POST /api/optimize
             Input: rendered notification_text + derived_reward
             Updates: digest generation prompt in llm/generation.py
             Artifact: optimization_runs doc → admin GEPA diff panel
             Known defect: the lift is synthetic (engagement_before × 1.12 when the
             prompt changed) and any changed prompt auto-activates via the
             engagement_delta > 0 gate — true by construction. R4a replaces this
             with measured gepa.optimize + treatment-arm deployment.
```

The two loops optimize different things: the **bandit** learns *when* to surface (timing policy, online); **GEPA** learns *how* the digest is phrased (language, offline). Neither touches model weights — honest scope is policy RSI + prompt RSI at the application layer.

**The gym is the shared evaluation infrastructure.** `sim/` runs the *real* `evaluate_surface(generate_digest=False)` path against synthetic personas and writes sim-tagged `feedback_events`, so the bandit genuinely converges and the dashboard curve is real — not injected. GEPA consumes the same feedback table plus fixed digest fixtures; it does not yet use a full per-decision LLM trace join.

---

## Observability: Two Telemetry Planes

| Plane | Signal | Standardizable? | Where it lives |
|-------|--------|-----------------|----------------|
| **Policy** | `should_surface`, `gate_reasons`, `adjusted_score`, bandit `α/β`, `context_class`, `derived_reward` | No OTEL vocab exists | `feedback_events`, `bandit_params`, `bandit_treatments`, EventBus |
| **LLM / agent** | enrichment, digest gen, harness tool calls — prompt labels, inputs/outputs when `GEMINI_LOG_IO` is enabled | Yes (OpenInference / OTEL GenAI) | `pipeline_events`, optional Gemini I/O log |

Current build uses **EventBus + persisted `pipeline_events`** for the demo trace, and `feedback_events.notification_text` as the GEPA training artifact. A future `decision_id` / OpenInference trace plane remains useful if we want per-token cost, prompt versioning, and exact prompt→output→reward joins.

---

## Persona Gym (`sim/`)

Simulated software-engineer lifestyles drive the real policy loop and the convergence data the dashboard shows.

| Module | Role |
|--------|------|
| `sim/persona.py` | `Persona(name, calendar_pattern, engagement_style, topic_weights, active_hours)`. Cast: **Alex** (SWE, regular cal, snoozes in meetings), **Maya** (ML eng, sparse, morning-engaged), **Jordan** (founder, dense, mostly dismissive) |
| `sim/context_sampler.py` | `sample_context(persona, day, tick)` → valid `ContextSnapshot` (varies gap, location, meeting density per pattern) |
| `sim/feedback_model.py` | `simulate_feedback(persona, cluster, context)` → `FeedbackAction` (topic fit × attention capacity × style noise) |
| `sim/gym.py` | `run_gym(personas, days, ticks)` → calls real `evaluate_surface(generate_digest=False)` and applies bandit reward; records engagement/day. Events tagged `run_id` for `sim reset` |

CLI: `kairos sim run --days 14 --personas alex,maya,jordan` · `kairos sim reset`. The gym calls `evaluate_surface(..., generate_digest=False)` so it skips the ~10–25s Gemini digest call across thousands of ticks (personas react to cluster topic + context, not prose). Gym writes sim-tagged events to live collections, so `/api/metrics` shows real convergence; `sim reset` clears sim docs for a clean live Act 2.

**Demo arc:** corpus prep -> live single-user feedback (dismiss -> beta update) -> persona gym convergence.

---

## Current Demo Build State

| Dimension | Score | Status | Gap |
|-----------|-------|--------|-----|
| Continual Learning thesis | 9/10 | ✅ Bandit + feedback loop + snooze label | Learning curve exists; annotate the “dismiss → β update” beat more explicitly |
| Self-Improvement Stack | 7/10 | ✅ GEPA shipped end-to-end | Admin GEPA panel skips silently when feedback < threshold; no readiness count shown |
| Differentiation from median | 8/10 | ✅ GAMBITTS-lite + cohort priors | Treatment-lift panel not in UI; cohort-prior activation not surfaced |
| Demo flow | 6/10 | ✅ Browser path exists | Gym and at least one GEPA diff should be pre-seeded before demo |
| Learning visibility | 6/10 | ✅ Bandit alpha/beta panel + GEPA diff | No single visual that shows the policy learned something; trend data exists but isn't annotated |

**Target: 10/10 all dimensions.** See [Finish Line Sprint](#finish-line-sprint-1010-checklist) below.

| Theme proof | Status | Artifact |
|-------------|--------|----------|
| Continual learning | ✅ Proven | `feedback_events` → `bandit_params`; dismiss increments β live |
| Treatment learning | ✅ Partial | `bandit_treatments` keyed by digest style; treatment-lift panel pending |
| Self-improvement stack | ✅ Proven | EventBus/SSE, persisted `pipeline_events`, `/api/metrics`, sim gym |
| Prompt self-improvement | ✅ Partial | `kairos optimize run/nightly`, `/api/optimize`; readiness indicator pending |
| Exact LLM trace join | 🚧 Future | `decision_id` / OpenInference-style trace plane |

## Research-Driven Roadmap (R1–R6 + R4a)

Force-multiplier upgrades distilled from two independent research passes (this repo's reasoning + the Exa-sourced survey in `docs/archive/research/CURSOR.md`).

**Selection criterion (revised).** This is an *unbounded* build — coding agents remove engineering effort as a constraint. So nothing is deferred for being "too much work," "med effort," or "won't show on stage." An item is only deferred when there is a **genuine technical reason** it should not be built now: a data-regime limit (the method is unsound at our sample size), an objective mismatch (it optimizes against the restraint thesis), a calibration failure, a degenerate-input problem, or a circular-validation trap. Every deferral below carries one of those reasons explicitly — see [Deferred — with technical rationale](#deferred--with-technical-rationale). Items previously buried on scope grounds (PRISM, LTV-POMDP) are promoted to active R-lines.

| # | Upgrade | Research basis | What it fixes | Primary files | Status |
|---|---------|----------------|---------------|---------------|--------|
| **R1** | GAMBITTS-lite — action vs. treatment | Generator-Mediated Bandits (2025); Action-Centered TS (Greenewald–Murphy, NeurIPS 2017) | ✅ Shipped secondary treatment posterior by `digest_style`; treatment-lift panel is Finish Line Sprint D | `db/bandit.py`, `core/feedback.py`, `web/app.py`, `index.html` | Done / Sprint D |
| **R2** | Linear Thompson Sampling | LinUCB (Li 2010 — news timing); Linear TS (Agrawal–Goyal 2013) | Discrete `context_class` buckets fragment sparse feedback; similar moments share zero signal | `core/bandit.py`, `db/bandit.py`, `core/moment.py`, `core/ranking.py` | Active |
| **R3** | Sleep-time-lite | Sleep-time Compute (Lin 2025); Letta dual-agent | Live SURFACE path is 20–40s (moment-fit + grounding + digest) | `core/sleep_cache.py`, `core/context.py`, `core/ranking.py` | Active |
| **R4** | GEPA + trace join | GEPA (Agrawal 2026); Letta Context Repositories | ✅ Prompt diff loop shipped; remaining trace join would make prompt→output→reward exact | `core/optimize.py`, `db/optimization_runs.py`, future trace table | Partial |
| **R4a** | Real GEPA — standalone `gepa` library | GEPA (Agrawal et al., ICLR 2026 oral); `gepa-ai/gepa` | Shipped loop fabricates its lift (`engagement_before × 1.12`) and auto-activates any changed prompt; no measured optimization exists | `core/optimize.py`, `core/eval_harness.py`, `db/optimization_runs.py`, `sim/persona.py` | Active |
| **R4b** | Exa grounding adapter | Exa API | ✅ Shipped — Exa retrieval + Gemini synthesis replaces `google_search` tool coupling; ahead of R2/R5 sequencing since it was small, contained, and unblocked | `llm/exa_client.py`, `llm/grounding.py` (`search_web`), `llm/generation.py` (`_ground_digest`), `llm/research.py` | Done |
| **R5** | PRISM — calibrated speak-vs-silent | PRISM (2026); selective prediction / calibrated abstention | Binary `moment_fit` threshold is uncalibrated; abstention is the thesis but isn't principled | `core/ranking.py` gate layer, `core/intelligence.py` | Active (promoted) |
| **R6** | Latent-receptivity POMDP / LTV | O'Brien 2022 (Meta); Steyvers–Mayer 2025; restless bandits | Myopic bandit optimizes this tick; the real failure is 7-day disengagement. Hand-tuned `daily_surface_budget` + `min_gap` are a crude approximation of the optimal long-horizon policy | `core/ltv.py` (new), `core/ranking.py` gates, `sim/feedback_model.py` | Active (promoted) |

**Evaluation substrate — doubly-robust OPE (DR-OPE).** Promoted from the deferred tail to underwrite everything above. Before deploying any new policy (R2 linear, R5 gate, R6 POMDP), DR-OPE estimates its engagement on *logged gym data without running it live*, combining the bandit's reward model with importance-weighted corrections. This turns R2/R5/R6 comparisons from "ship and eyeball the sparkline" into a rigorous counterfactual estimate, and gives the gym A/B a defensible number. Build it as `core/ope.py` once R2 lands so the linear-vs-Beta comparison is its first customer.

**Performance substrate — prove before porting.** Rust/PyO3 is not a roadmap item until traces prove Python/native-backed paths are the bottleneck. Add a `perf_spans`/`llm_traces` layer that records `decision_id`, span name, wall time, CPU time where available, item counts, provider/model, cache hit/miss, and error. Capture at least 100 demo/gym heartbeats and one full `bookmarks prep` run before deciding on a port.

| Gate | Evidence required | Python-first fix | Rust/PyO3 only if |
|------|-------------------|------------------|-------------------|
| Live heartbeat | p50/p95 by span: context, query embedding, vector rank, bandit fetch, moment-fit, digest, Exa grounding, DB writes | Sleep cache; async Exa; digest cache; fewer sequential LLM calls | CPU-only Python span is >25% of p95 after network/LLM/DB time is excluded |
| Local vector fallback | cluster/bookmark count, vector dimension, cosine rank wall/CPU time | NumPy matrix cosine; libSQL/Turso vector search | NumPy/SQL still misses target at >50k local vectors |
| Prep pipeline | per-stage timings: link fetch, Exa, Gemini, embedding, HDBSCAN, writes | bounded concurrency; content hash cache; bulk writes; avoid full recluster | CPU-bound parsing/vector math dominates after I/O is cached |
| Trace/event processing | events/sec, payload bytes, write latency | batch persistence; TTL; compact JSON payloads | serialization/compression is a proven hot path |

Decision rule: do not introduce a Rust module unless the trace shows a repeatable CPU-bound hotspot, a Python/native alternative has been tried or ruled out, and the candidate module has a narrow boundary (`vector_rank`, `url_normalize`, `trace_codec`, or feature-vector construction). If the slow span is LLM, Exa, embeddings API, database I/O, or scheduler latency, fix orchestration/cache/provider behavior in Python instead.

### R1 — GAMBITTS-lite (the standout — both passes converged here)

The thesis split made learnable: an interrupt is **action** (which cluster) × **treatment** (the digest the user actually saw). Kairos now updates both the cluster posterior and a secondary `bandit_treatments` posterior keyed by `digest_style`, so GEPA rewrites can become measurable as treatments.

- Current: update the posterior on `feedback_events` using both the cluster and observed digest style.
- Next: expose treatment lift in Admin (`grounded` vs `runtime_fast` vs future prompt versions).
- **Why it matters:** it bridges the bandit loop and the GEPA loop — prompt rewrites become measurable as a treatment effect rather than a vibes-based copy change.

### R2 — Linear Thompson Sampling (the bandit-quality upgrade both surveys under-weighted)

Replace per-bucket `Beta(α,β)` with a reward model **linear in a continuous context feature vector** `x` (gap, density, post-meeting, `topical_affinity`, hour), optionally crossed with the cluster embedding. Maintain a Gaussian posterior over weights; Thompson-sample from it. A click in `desk_long_gap_work` now informs `cafe_long_gap_work` because features overlap — the right-sized fix for sparse feedback. Linearity is a **calibration choice, not a budget one**: the linear-Gaussian posterior is conjugate and therefore exactly calibrated at any sample size, which Thompson sampling requires (see VITS deferral for why neural breaks this).

- Ship feature-flagged alongside the Beta bandit so the **gym can A/B the two** (`sim/gym.py` already replays the real policy), scored by DR-OPE.
- Retire `context_class` discretization (`core/moment.py`) as the bandit key once linear is validated; keep it for snooze TTL lookup.

### R3 — Sleep-time-lite (the cheap latency win)

Pre-materialize the expensive intelligence while idle so heartbeats stay fast. **Cheap version only** — not the full dual-agent system:

- `core/sleep_cache.py::build_surface_cache(user_id, context)` → top clusters + digest drafts + moment-fit hints, fingerprinted + `expires_at`.
- Trigger on headspace sync / `POST /api/context/fuse` / cron — **not** every heartbeat. Invalidate on calendar change, fatigue/snooze delta, or fingerprint mismatch.
- Pair with defaulting `INTELLIGENCE_MOMENT_FIT_CHECK=false` for the demo (removes the 2nd sequential Gemini call). `moment_narrative`+TTL is already a partial implementation to build on.

### R4 — GEPA + trace join (Recursive-Intelligence coverage)

The offline prompt-RSI loop (see [Two Self-Improvement Loops](#two-self-improvement-loops)). `core/optimize.py` runs a reflective pass over the digest prompt, emitting a real prompt diff into `optimization_runs` and the admin GEPA panel — but its lift number is synthetic and its activation gate is vacuous; **R4a replaces the loop's optimizer and deployment path**, after which R4's remaining research-grade upgrade is the exact trace join: prompt version + model input + model output + reward for every decision. `kairos optimize nightly` stays cron-safe and skips when feedback is insufficient.

### R4a — Real GEPA via the standalone `gepa` library (replace the hand-rolled loop with a measured optimizer)

**What changed and why.** The previous R4a proposed a BAML migration of the LLM boundary. That is demoted to the deferred table: the boundary already works — Gemini constrained decoding + Pydantic + the `$ref` inliner shipped 2026-07 — and BAML's headline features (schema-aligned parsing of malformed output, multi-language codegen) solve problems Kairos doesn't have. The real defect at the LLM layer is in the *optimization* loop, not the boundary: `core/optimize.py` fabricates its measured lift (`engagement_after = engagement_before × 1.12` whenever the prompt changed) and `db/optimization_runs.get_active_prompt()` activates any run with `engagement_delta > 0` — a gate that is true by construction. The shipped loop is prompt hot-swapping with an invented success number. R4a replaces it with the real optimizer the loop was named after.

**Tool choice — standalone `gepa`, not full DSPy, not BAML (validated against the field 2026-07):**

- GEPA (Agrawal et al., ICLR 2026 oral) outperforms MIPROv2 by ~10–13% and GRPO-style RL by up to 20% with ~35× fewer rollouts, and is the core algorithm inside Comet's Opik Agent Optimizer — the field converged on reflective prompt evolution.
- It ships standalone (`pip install gepa`): `gepa.optimize(seed_candidate={...})` takes a plain dict of prompt strings plus an adapter. **No DSPy module rewrite; the Gemini/Pydantic serving path is untouched.** Full DSPy adoption (`dspy.Module` at runtime) is explicitly a non-goal — DSPy earns its keep when the whole pipeline is compiled; ours is one prompt behind `prompt_override`.
- Alternatives rejected: **MIPROv2** — jointly optimizes few-shot demo slots we don't use, and is outperformed by GEPA on instruction-only tasks; **TextGrad** — strong on uniform-difficulty tasks, but our eval set is heterogeneous persona × context pairs, exactly where GEPA's Pareto-frontier candidate selection wins; **promptim / LangSmith** — LangChain-native, wrong stack; **Opik Agent Optimizer** — wraps GEPA, adds a platform dependency for no algorithmic gain.

**The metric prerequisite (the honest hard part).** GEPA needs a metric that varies with prompt wording, and neither existing evaluator qualifies: `sim/feedback_model.simulate_feedback` maps persona × cluster × context → action *without ever reading digest text* (zero gradient on wording), and `core/eval_harness.run_fixture_eval` reads the text but only checks structure (3 fixtures, length thresholds). Step one is upgrading the harness into a judge:

1. Expand `FIXTURES` to ~20–30 persona × context × cluster triples, reusing `sim/persona.py` personas (train/held-out split).
2. Add `judge_digest(fixture, digest)` — an LLM-judge rubric scoring persona-fit (would *this* persona at *this* moment engage?), restraint compliance (no curiosity-gap framing, no overpromising `why_now`; the anti-clickbait check the reward table alone can't enforce), with the structural checks kept as hard fails. It returns `{score, feedback}` — the textual feedback is what GEPA reflects on, and is the API's whole advantage over scalar-reward optimizers.

**Sketch** (~150 lines, mostly adapter code):

```python
import gepa
from kairos.core.eval_harness import judge_digest, load_fixture_split  # new
from kairos.llm.generation import _DEFAULT_DIGEST_PROMPT, generate_cluster_digest

class KairosGymAdapter(gepa.GEPAAdapter):
    def evaluate(self, batch, candidate, capture_traces=False):
        digests = [
            generate_cluster_digest(
                cluster_id=f"gepa-{fx.name}",
                cluster_name=fx.cluster_name,
                cluster_summary=fx.cluster_summary,
                bookmark_snippets=fx.snippets,
                context=fx.context,
                member_count=len(fx.snippets),
                prompt_override=candidate["digest_prompt"],
            )
            for fx in batch
        ]
        # judge returns score ∈ [0,1] + feedback text, e.g.
        # "persona=snoozer, ctx=8min gap: why_now promises a deep read — mismatch"
        return [judge_digest(fx, d) for fx, d in zip(batch, digests)]

    def make_reflective_dataset(self, candidate, eval_batch, components):
        ...  # judge feedback strings grouped per component

train_fx, heldout_fx = load_fixture_split()
result = gepa.optimize(
    seed_candidate={"digest_prompt": _DEFAULT_DIGEST_PROMPT},
    trainset=train_fx,
    valset=heldout_fx,
    adapter=KairosGymAdapter(),
    reflection_lm="gemini/gemini-2.5-pro",  # reflection deserves the big model
    max_metric_calls=150,                   # ≈ fixtures × ~6 candidate evals
)
```

**Deployment — through the treatment bandit, not a hot swap.** Delete the ×1.12 estimate and the `engagement_delta > 0` activation path. The winning `result.best_candidate["digest_prompt"]` is registered as a treatment arm `digest_style="gepa_v{n}"` in `bandit_treatments` (R1 infrastructure, already shipped); live traffic splits between incumbent and candidate, and `apply_treatment_reward` promotes on real posterior separation. `optimization_runs` stores the prompt diff plus the judge's held-out score labelled *offline/simulated* — never a claimed live engagement delta. This makes the two-loop story literally true: **GEPA proposes on simulated users; the treatment bandit disposes on real ones.**

**Named caveat.** Optimizing against the judge means GEPA learns to please the judge — the same circularity trap named in R6. That is why activation authority lives with the live treatment bandit, and why the judge rubric encodes restraint, so "pleasing the judge" at least points at the thesis rather than at engagement bait.

### R4b — Exa grounding adapter (shipped — decoupled from optimizer work)

The grounding swap from the old R4a, kept as its own line so a retrieval-provider migration is never coupled to the learning loop. Replaced direct Gemini `google_search` tool calls with a two-step pipeline: `llm/exa_client.py::get_exa_client()` (shared singleton, mirrors `get_genai_client()`) + `llm/grounding.py::search_web(query, num_results)` calls Exa's `/search` endpoint with `contents={"text": ..., "highlights": true}`, returning a provider-agnostic `GroundedText(text, citations)`; a plain (tool-less) Gemini call then synthesizes that retrieved text into `digest.web_context`, respecting the digest's voice — `llm/generation.py::_ground_digest` and `llm/research.py::research_bookmark` both consume it. `GROUNDING_PROVIDER=exa` (default) / `none`, `DIGEST_USE_WEB_GROUNDING` gates digests, `digest_skip_grounding_evergreen` unchanged. Fails soft: no `EXA_API_KEY`, a rate limit, or an empty result set returns `GroundedText("", [])` rather than raising — grounding is enrichment, not a required step, so offline/CI runs work with zero config. Citations, published dates, and highlight text come straight from Exa's response — no more annotation-parsing off Gemini's grounding metadata.

**Why shipped ahead of R5/R2 despite the stated sequencing:** the original ordering deferred R4b because it "carries integration risk with zero learning-loop payoff." In practice it was small, fully decoupled from the bandit/GEPA loops, and had zero open questions once Exa's API shape was confirmed — pulling it forward cost nothing and removed a live dependency on Gemini's `google_search` tool coupling before R5/R2 land.

### R5 — PRISM, calibrated speak-vs-silent (promoted — silence is the thesis, so make it principled)

"Silence is a feature" is, formally, a **calibrated-abstention problem under asymmetric costs** — and Kairos currently resolves it with a binary `moment_fit` boolean plus a hand-set score threshold, which is exactly the uncalibrated heuristic PRISM replaces.

- Fit a calibrated speak/silent head that estimates `P(engage | moment, cluster)` with a *reliability guarantee*, then surface only when expected benefit exceeds the asymmetric interruption cost.
- The one input it needs is the cost matrix — `cost(false_surface)` vs `cost(false_silence)` — which is a hyperparameter we own and can sweep in the gym, **not** a data-regime blocker. Calibrating one binary head needs far less data than a contextual bandit, so this is sound now.
- **Why promoted:** it was previously dropped on scope grounds. There is no technical reason to defer it, and it is the single most thesis-aligned item in the survey. Replaces the binary gate in `core/ranking.py` / `core/intelligence.py`.

### R6 — Latent-receptivity POMDP / LTV (promoted — the long-horizon moat, with a non-circular gym)

Thompson sampling and the fatigue gate both optimize *this tick*. The real failure mode is tomorrow's disengagement — too many surfaces and the user learns to ignore or disables the agent. Model a latent **receptivity** state and choose SURFACE vs KAIROS_OK to maximize multi-day engagement, refining or replacing the hand-tuned `daily_surface_budget` + `min_gap`.

- **The trap, named:** learning the latent dynamics from `sim/feedback_model.py` and then validating on the same simulator is circular — you'd only confirm your own assumptions. So the precondition is a methodologically honest gym: the simulator's receptivity process must be **structurally different from, and hidden from, the policy**, so the POMDP has to *recover* it from observed behavior. Building that harder gym is part of R6, not a reason to skip it.
- **Why promoted:** the research called this the biggest moat; it was buried as "post-traction." Under an unbounded build the circular-validation risk is an engineering problem to solve, not a reason to defer. New `core/ltv.py` + gym redesign.

### Deferred — with technical rationale

These stay out of the active roadmap, but **not** for effort reasons. Each has a concrete technical reason that survives the unbounded framing:

| Item | Research | Technical reason to defer (not scope) |
|------|----------|----------------------------------------|
| **VITS / neural contextual bandit** | ICML 2024 | **Calibration failure at our sample size.** Thompson sampling's regret guarantee needs a *calibrated* posterior. Neural bandit posteriors (MC-dropout, last-layer Bayes, ensembles) are miscalibrated below ~10³–10⁴ events, breaking explore/exploit — premature collapse or pure noise. R2's linear-Gaussian posterior is conjugate and exactly calibrated now. Revisit only after thousands of real feedback events. |
| **Graph spreading activation** | SYNAPSE / PersonalAI | **Degenerate input.** Spreading activation propagates over a weighted item/cluster graph; our corpus is one 51-member mega-cluster + 41 noise singletons — a near-clique plus disconnected points. Propagation either saturates uniformly or doesn't move, collapsing back to base cosine. Adds signal only once the corpus develops genuine multi-cluster link topology. Re-test as corpus grows. |
| **TIM intra-day scheduling** | Kuaishou 2024 | **Objective mismatch with the thesis.** TIM's loss maximizes aggregate slot-wise CTR given a notification budget — it is trained to *fill slots well*. Kairos's objective rewards correct *abstention*. Adopting it reintroduces a throughput-maximizing allocator that fights restraint. Not a future "do later" — a "do not, by design," unless the product thesis changes. |
| **Delayed-feedback bandit (Bootstrap TS)** | UAI 2024 | **Throughput regime suppresses the pathology — conditionally.** By Little's law, in-flight uncensored rewards ≈ arrival-rate × mean-delay. Live: ~3 surfaces/day (0.125/hr) × minutes-to-hours delay ⇒ ≈0.1 expected premature updates — the noise Bootstrap-TS fixes barely exists *because the restraint budget keeps throughput tiny*. **Caveat:** the gym runs at compressed high throughput, so if gym pretraining models realistic delay (it currently applies reward synchronously), Bootstrap-TS *would* matter there. Build it iff the gym is upgraded to model delay; skip for the live path on throughput grounds. |
| **Recharging / restless bandits for habituation** | — | Partially **subsumed** by R6. Habituation (engagement decaying under repeated exposure) is a special case of the latent-receptivity state R6 models. Build standalone only if R6's POMDP proves too heavy and a lighter restless-bandit approximation is wanted. |
| **BAML typed LLM boundary** | BoundaryML BAML | **Solves a problem the boundary no longer has.** BAML's value is schema-aligned parsing of malformed LLM output, multi-language codegen, and day-1 support for models without native structured output. Kairos gets valid JSON from Gemini constrained decoding, the `$ref` inliner (shipped 2026-07) removed the schema brittleness that motivated the migration, and there is exactly one consumer language. A `.baml` DSL + codegen step is a lateral replatform of a working path — pure integration risk, no capability gain. Revisit iff Kairos goes multi-provider/multi-language or drops constrained decoding. |

Full survey + citations: `docs/archive/research/CURSOR.md`.

**Active roadmap sequence:** R4b (Exa grounding — shipped out of order, see rationale above) -> R4a (real GEPA — deletes the fabricated-lift gate; judge harness first, then `gepa.optimize`; offline only, serving path untouched) -> R5 (calibrated gate — small, thesis-critical) -> R2 (linear bandit) -> DR-OPE substrate -> R6 (POMDP + honest gym) -> R3 (sleep cache) -> R4 trace join. R1 is shipped; its treatment-lift panel is Finish Line Sprint D — and it is also R4a's deployment channel, so Sprint D doubles as GEPA's activation UI.

---

## Delivery Layer (`delivery/`)

`HeartbeatService` calls `deliver(result, notification, mode)` which fans out to configured adapters.

| Adapter | What it does | Config |
|---------|-------------|--------|
| `WebDeliveryAdapter` | Emits `notification` event to `EventBus` → SSE → dashboard inbox | `delivery_targets=web` |
| `OSDeliveryAdapter` | `terminal-notifier` (macOS) or `notify-send` (Linux), best-effort | `os_delivery_enabled=true` |
| `return_only` mode | No side effects — HeartbeatResult only (FastMCP callers) | `delivery=return_only` |

`DeliveryHints` on the result tells MCP/Antigravity host agents:
- `rendered_markdown`: pre-rendered digest for host transcript
- `dashboard_url`: deep link to `GET /n/{notification_id}`
- `suggested_host_actions`: e.g. "show rendered_markdown to user", "call record_feedback"
- `suppress_ok_in_chat`: KAIROS_OK is silent — no chat spam when gate stays closed

---

## Interfaces

### Web Dashboard (FastAPI + SSE)

Single page, two panes. `EventBus` is already wired — FastAPI SSE endpoint streams it to the browser.

```
┌─────────────────────────┬──────────────────────────────────┐
│   UPCOMING / HISTORY    │   AGENT ACTIVITY (live via SSE)  │
│                         │                                  │
│  ● 2:30pm               │  14:22 scored 8 clusters         │
│    Distributed Systems  │  14:22 infra-arch → 0.84 ✓       │
│    [before your mtg]    │  14:22 gate: gap=42m ✓ budget ✓  │
│                         │  14:22 → surfaced digest         │
│  ✓ 11am — engaged       │                                  │
│  ✗ 9am — dismissed      │  LAST SLEEP-TIME PASS            │
│                         │  prompt diff (summary v3→v4):    │
│  Engagement rate        │  - "Here are relevant threads"   │
│  ████████░░ 74% (+12%)  │  + "Before {event}, these {n}…"  │
│                         │  engagement: 61% → 74% ✓         │
│  [Manage clusters]      │                                  │
│  [Restraint budget: 3]  │                                  │
└─────────────────────────┴──────────────────────────────────┘
```

**Admin learning view.** The activity feed is the spine: it shows context reads, ranking, gates, digest generation, delivery, and feedback as SSE events. Side panels show live context, clusters, engagement trend, bandit α/β, and GEPA prompt diffs. A future trace inspector can turn persisted `pipeline_events` into a per-decision rail with model latency and prompt/output joins.

```
│ ● context   desk·gap42m·dens0.3      2ms
│ ● match     →distributed-sys 0.84   18ms
│ ● sample    Beta(12.4,4.1)→0.75      0ms
│ ● gate      4/4 PASS                 1ms
│ ◆ generate  flash·prompt v2     2,310ms   ← amber; ⌄ prompt/output
│ ● deliver   web · notif 7f3a         4ms
│ ◇ outcome   dismissed −0.4     +6m later
│            bandit β 4.1 → 4.5
```

`kairos serve` ✅ ships the FastAPI app (`web/app.py`): SSE, inbox, `/api/demo/surface`, `/api/feedback`, `/api/bandit`, `/api/metrics`, `/api/optimize`, `/api/prep/start`, and `/api/prep/{job_id}`. Remaining optional polish: `/api/trace/latest` and treatment-lift visualization.

### MCP Server (FastMCP)

Tools in `agent/tools.py` are already written as plain Python functions — dual-use for Antigravity harness and FastMCP. The MCP server is a thin wrapper exposing `ALL_TOOLS`.

```python
# tools already exist:
get_current_context()
get_relevant_bookmarks(query, limit)
get_cluster_summary(topic)
run_heartbeat(delivery, context_override)
record_feedback(notification_id, action, url)
add_bookmark(url, notes)
```

Claude Code `/loop` calls `run_heartbeat` via MCP every 5 min during the demo. The session transcript IS the observability.

### Google Workspace MCP — Calendar Context (replaces manual Calendar API)

The Google Workspace MCP server provides `list_events`, `get_event`, `suggest_time` and 5 more calendar tools over OAuth2. This replaces implementing `google-api-python-client` + credential flow manually in `context.py`.

Wire into the Antigravity agent config as an MCP connector, then the agent calls `list_events` as a tool directly. The context sensor becomes: call `list_events` for today → parse gap, density, upcoming title → build `ContextSnapshot`.

Auth: Google Cloud project → OAuth2 client ID + secret → authorized redirect URI. One-time setup, much lighter than implementing the Calendar API from scratch.

---

## Scheduling

Custom scheduler eliminated. Three Claude Code mechanisms replace it:

| Job | Mechanism | Interval |
|-----|-----------|----------|
| Context poll + heartbeat (demo) | Claude Code `/loop 5m` → MCP `run_heartbeat` | 5 min |
| Context poll + heartbeat (prod) | Desktop Scheduled Task → `kairos heartbeat` | 5 min |
| Nightly GEPA pass | `kairos optimize nightly` / `just optimize-nightly` / Cloud Run cron | Daily 2am |

---

## Stack

| Component | Tool | Status |
|-----------|------|--------|
| Persistence | Turso/libSQL — local `kairos.db` file, optional hosted-primary sync | done |
| Vector search | libSQL native `vector_distance_cos` with in-memory fallback | done |
| Embeddings | Gemini default; local BGE optional | done |
| Clustering | HDBSCAN + stable centroid reuse | done |
| Bandit | Thompson sampling α/β + cohort prior + treatment posterior | done |
| Prompt optimization | Hand-rolled reflection loop (ungated) → standalone `gepa` library + LLM-judge harness, treatment-arm deployment (R4a) | replace |
| LLM boundary | Gemini constrained decoding + Pydantic + `$ref` inliner (BAML evaluated and deferred — see roadmap) | done |
| Web grounding | Exa `search_web()` retrieval + Gemini synthesis (`GROUNDING_PROVIDER=exa`); fails soft to `none` without `EXA_API_KEY` (R4b) | done |
| LLM — enrichment | Gemini flash-lite via `google-genai` Interactions API | done |
| LLM — digest generation | Gemini flash via `google-genai` Interactions API | done |
| Agent harness | Antigravity SDK (`google-antigravity`) | done |
| Observability | EventBus in-process pub/sub → SSE | done |
| Delivery — web | WebDeliveryAdapter → EventBus | done |
| Delivery — OS | OSDeliveryAdapter (terminal-notifier / notify-send) | done |
| Calendar/Gmail | Google OAuth + Kairos MCP/ADK fuse paths | done |
| Geo | Manual/geofence-style context anchors | partial |
| Ingest | X API `GET /2/users/{id}/bookmarks` | done |
| API backend | FastAPI + SSE (`kairos serve`) | done |
| MCP server | FastMCP wrapping Kairos tools | done |
| Scheduling | `/loop`, local cron, Cloud Run cron-safe commands | partial |

---

## X Bookmark Access — De-risk in Hour 1

X API bookmark endpoint: OAuth2 user-context, rate-limited, pricing volatile. Strategy:
- Primary: live sync via `GET /2/users/{id}/bookmarks` with expansions (author, referenced tweets)
- Bootstrap / fallback: X data export (Settings → Download an archive)
- Demo: export is sufficient if API setup burns time

**Verify API access before architecting around it.**

---

## Demo Strategy — No Real Feedback in 48h

1. Seed **synthetic persona** with scripted preferences
2. Simulate 2 weeks of `feedback_events` to populate the database
3. Show engagement-rate curve climbing (SQL aggregation → Chart.js)
4. **One live adaptation on stage**: wrong-context surface → dismiss → bandit update → better surface

Be explicit: real learning takes weeks; the simulator compresses it to 3 minutes.

---

## What's Done vs. Next

### Shipped
- Turso/libSQL repositories for bookmarks, clusters, notifications, feedback, bandit params, treatment params, context cache, Google tokens, prep jobs, pipeline events, and optimization runs.
- X OAuth + incremental bookmark sync; `kairos bookmarks prep` for enrich → research → embed → cluster.
- Fixed embedding space with Gemini default and local BGE optional; HDBSCAN clustering with centroid reuse.
- Policy core: headspace preparation, vector ranking, Thompson sampling, hard gates, snooze filtering, digest generation, and `KAIROS_OK` as a first-class outcome.
- Online learning: dismiss/snooze/click feedback writes `feedback_events` and updates bandit α/β.
- Treatment learning: digest style writes a secondary `bandit_treatments` posterior.
- Dashboard: `/api/demo/surface`, SSE admin feed, inbox feedback, metrics sparkline, bandit panel, GEPA panel, and prep jobs.
- MCP + ADK paths: direct policy tools via Kairos MCP; optional ADK `--via-agent` path for Workspace MCP sensor fusion.
- Self-improvement: persona gym, `/api/metrics`, `kairos optimize run|readiness|eval|nightly`, `/api/optimize`, and `optimization_runs`.

### Remaining polish
See [Finish Line Sprint](#finish-line-sprint-1010-checklist) for implementation details.

1. **Seed gym + research:** `kairos sim run --days 7` → populates sparkline + GEPA feedback pool. `just demo-corpus` (≥ 20 bookmarks) → populates researched link cards.
2. **Build trend annotation + snooze callout** (Continual Learning → 10): sparkline slope + `rate_change_pct` badge; SSE snooze event with timing-label semantics.
3. **Build treatment-lift mini-panel** (Differentiation → 10): compact table in Admin showing `p_engage` by `digest_style` from `bandit_treatments`.
4. **Build GEPA readiness indicator** (Self-Improvement → 10): show feedback count + min-required in GEPA panel before any run; load via `GET /api/optimize/readiness`.
5. **Wire gym seed into demo-serve** (Demo flow -> 10): `just demo-serve` auto-runs `just demo-seed-gym` when `feedback_events` collection is empty.

### Beyond the research roadmap

The policy/intelligence research work is tracked as active R-lines (R2-R6 plus R4a) in the [Research-Driven Roadmap](#research-driven-roadmap-r1r6--r4a). What sits outside that roadmap, gated by **external dependencies** rather than effort:

- **More ingest sources** (Readwise, Pocket, browser export) — each needs a separate third-party API/account integration; real external dependency, not internal work.
- **Live longitudinal validation** of R6's POMDP and the delayed-feedback path — requires real users over real days; the gym can pressure-test the mechanism but cannot substitute for longitudinal ground truth (see the circular-validation note under R6).

---

## Three Things Before Anyone Else Starts

1. **Eval harness before the bandit.** No yardstick = no demo. Build the fixed test set from the synthetic persona in hour 1.
2. **Snooze capture before hour 3.** Right-thing-wrong-time is the most informative timing label. Every other team will miss it.
3. **Make learning visible.** The optimization_runs prompt diff — rendered in the dashboard — is worth more on stage than any accuracy number. Show the machine editing itself.

---

## Finish Line Sprint (10/10 checklist)

Five contained additions that close each gap. Ordered by dependency — E first because it populates the data all other panels depend on.

---

### E — Auto-seed gym in demo-serve + research floor -> Demo flow 10/10

**What:**
1. In `Justfile`, update `demo-prep` to check whether `feedback_events` has sim events; if empty, run the gym automatically. Gate behind `SKIP_GYM` (already the convention):
   ```bash
   # In demo-prep recipe — after corpus step, before serve:
   if [[ "${SKIP_GYM:-0}" != "1" ]]; then
     echo "▸ Seeding persona gym (7 days × 3 personas)…"
     uv run kairos sim run --days 7 --personas alex,maya,jordan
   fi
   ```
2. Add `DEMO_RESEARCH_LIMIT=20` to `.env.demo` so `just demo-corpus` always researches at least 20 bookmarks (currently unset — research silently skips).

**Why:** Every other panel depends on `feedback_events` being populated: sparkline needs days of data, GEPA needs a feedback pool, treatment-lift needs per-style events. Without auto-seeding, `just demo-serve` on a fresh clone produces an empty admin that tells no story. One command → everything works.

---

### A — Learning curve trend annotation → Continual Learning 10/10

**What:** In `index.html` `refreshMetrics()`, compute a linear regression slope over `sparkline[]` (5 lines of vanilla JS — no library). Render a badge above the bars:

```
↑ +18% engagement trend   or   ↓ −5% engagement trend
```

Also surface the existing `rate_change_pct` from `/api/metrics` as a `"last 7d vs prior 7d"` chip in the sparkline subtitle. The value is already computed in `db/metrics.py::rate_change_pct()` and returned in the payload.

**Why:** Judges see raw bars and don't mentally connect them to a learning story. An annotated slope turns the chart from a histogram into a policy convergence curve — the central claim of Continual Learning made visible in 2 seconds.

---

### B — Snooze-as-label SSE callout + UI tooltip → Continual Learning 10/10

**What:**
1. In `core/feedback.py`, in the snooze branch (~line 112), add an SSE emit after the cluster re-queue:
   ```python
   event_bus.emit(
       "intelligence",
       f"Snooze stored as timing label for «{ctx_class}» — "
       "cluster queued for next matching context; no topic penalty applied.",
       snooze=True, cluster_id=record.cluster_id, ctx_class=ctx_class,
   )
   ```
2. In `index.html`, add `title="Tells the bandit: right topic, wrong moment — no cluster penalty"` to the Snooze 2h button.

**Why:** The snooze reward is `None` in `rewards.py` — the most deliberate design decision in the whole reward table. Judges watching the admin feed currently see a snooze fire and have no idea it's semantically distinct from dismiss. One SSE line makes it legible without any screen time.

---

### C — GEPA readiness indicator → Self-Improvement Stack 10/10

**What:**
1. Add `GET /api/optimize/readiness` to `web/app.py` — calls `feedback_readiness(days=14)` from `core/eval_harness.py`, returns `GepaReadiness` JSON.
2. In `index.html` GEPA panel, call this endpoint in `init()` and display:
   - `gepa_ready=false`: amber chip — *"12 / 30 events — run gym to enable GEPA"*
   - `gepa_ready=true`: green chip — *"Ready — 47 events collected"*

State: add `gepaReady: null, gepaReadyCount: 0, gepaMinSamples: 30` to the Alpine component.

**Why:** Currently the GEPA panel looks the same whether there are 0 or 200 feedback events. Judges who click "Run optimization" on a cold corpus get a silent skip. The readiness chip explains why it's not running, tells them what to do, and turns green after the gym seeds — itself a signal that real feedback has been collected and the self-improvement loop is primed.

---

### D — Treatment-lift mini-panel → Differentiation 10/10

**What:**
1. Add `GET /api/bandit/treatments` to `web/app.py` — queries `bandit_treatments`, groups by `digest_style`, computes `p_engage = alpha / (alpha + beta)`, returns sorted by `p_engage` desc.
2. In `index.html` admin Bandit panel, below the existing α/β display, add a compact table:
   ```
   Treatment lift (digest style)
   ─────────────────────────────────────
   grounded        ████████░░  p=0.74  n=23
   context_primed  ██████░░░░  p=0.61  n=18
   standard        ████░░░░░░  p=0.48  n=31
   evergreen       ███░░░░░░░  p=0.39  n=12
   ```
   Render each row as a mini progress bar (`width: p*100%`) and `p_engage` + sample count text.

**Why:** GAMBITTS-lite is the single feature that bridges the bandit loop and the GEPA loop — prompt rewrites become measurable as treatment effects rather than vibes-based copy changes. Without a visible panel, judges see sophisticated code but get nothing on stage. This table turns it into a talking point: *"The grounded treatment wins by 26 points — that's why GEPA rewrites toward web-grounded rationales."*

**Where:** `web/app.py` (one GET route); `index.html` (`treatments: []` state + fetch in `init()` + render block below bandit panel).

---

### Completion order and estimated effort

| Step | File(s) | Effort |
|------|---------|--------|
| E — gym auto-seed | `Justfile`, `.env.demo` | 15 min |
| A — trend annotation | `index.html` | 20 min |
| B — snooze callout | `core/feedback.py`, `index.html` | 15 min |
| C — GEPA readiness | `web/app.py`, `index.html` | 25 min |
| D — treatment panel | `web/app.py`, `index.html` | 30 min |

Total: ~105 min of implementation. All contained; no schema changes; no new collections.

---

## Pitch Frame

> Most second brains are write-only graveyards. You bookmark 500 things and read 12. Kairos doesn't make you a better searcher — it becomes a better interrupter. It learns that you never read long ML threads on back-to-back days but devour them Sunday morning at a coffee shop. It learns that "not now" on a ramen rec means "remind me when I'm near Chinatown with an hour free." Every night it rewrites the prompt it uses to describe your bookmarks, based on what you actually engaged with. The policy improves without you lifting a finger. Passive hoarding becomes execution.

---

## References

- GEPA (Agrawal et al., ICLR 2026 oral): https://arxiv.org/abs/2507.19457
- GEPA standalone library (`pip install gepa`, adapter + `optimize_anything` APIs): https://github.com/gepa-ai/gepa
- GEPA in DSPy (reference for metric-with-feedback signature): https://dspy.ai/api/optimizers/GEPA/overview/
- GEPA in production, test-driven approach (Decagon): https://decagon.ai/blog/optimizing-gepa-for-production
- MIPROv2 (Opsahl-Ong et al., 2024 — rejected alternative): https://arxiv.org/abs/2406.11695
- TextGrad (Yuksekgonul et al., 2024 — rejected alternative): https://arxiv.org/abs/2406.07496
- BAML / BoundaryML (evaluated, deferred): https://github.com/BoundaryML/baml
- Exa API docs: https://exa.ai/docs/llms.txt
- Contextual bandits / LinUCB (Li et al., WWW 2010): https://arxiv.org/abs/1003.0146
- Letta sleep-time compute (prior art): https://www.letta.com/blog/sleep-time-compute/
- Turso / libSQL AI & embeddings (native vector search): https://docs.turso.tech/features/ai-and-embeddings
- Google Workspace MCP: https://developers.google.com/workspace/guides/configure-mcp-servers
- Claude Code scheduled tasks: https://code.claude.com/docs/en/scheduled-tasks
