<div align="center">

# Kairos

**A contextual bandit for bookmark surfacing.**

Kairos learns **when** to interrupt with a topic cluster, not just **what** matches a query.
Silence is the default; feedback changes the policy.

<p>
  <a href="docs/ARCHITECTURE.md"><img alt="Architecture" src="https://img.shields.io/badge/system-architecture-2563eb?style=for-the-badge"></a>
  <a href="docs/DEVELOPER_GUIDE.md"><img alt="Developer guide" src="https://img.shields.io/badge/contributors-guide-059669?style=for-the-badge"></a>
  <a href="docs/MCP_SETUP.md"><img alt="MCP setup" src="https://img.shields.io/badge/MCP-setup-6b7280?style=for-the-badge"></a>
</p>

<p>
  <img alt="Python" src="https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-SSE-009688?style=flat-square">
  <img alt="Turso" src="https://img.shields.io/badge/Turso-libSQL%20vector%20search-4ff8d2?style=flat-square">
  <img alt="Gemini" src="https://img.shields.io/badge/Gemini-Interactions%20API-4285f4?style=flat-square">
  <img alt="MCP" src="https://img.shields.io/badge/MCP-Claude%20%2F%20Cursor-6b7280?style=flat-square">
</p>

</div>

---

## Why This Exists

Most bookmark tools become searchable graveyards. They can retrieve the right link when you ask, but they do not learn whether this is the right moment to interrupt.

Kairos treats surfacing as a policy problem:

1. Ingest and cluster saved bookmarks.
2. Read the current headspace: calendar gap, location, work mode, attention capacity.
3. Rank clusters by topical fit.
4. Thompson-sample learned engagement weights.
5. Surface one digest or return `KAIROS_OK`.
6. Update the bandit from snooze, dismiss, expand, and click feedback.

The core demo beat is simple: **dismiss a surface and watch beta increase**. No LLM fine-tuning, no hidden gradient steps.

---

## Quick Start

```bash
cp .env.example .env   # set GEMINI_API_KEY, optionally EXA_API_KEY for web grounding (storage is a local kairos.db file — no setup needed)
brew install just      # or: cargo install just

just demo-serve
```

Open [http://127.0.0.1:8420](http://127.0.0.1:8420), click **Surface now**, dismiss the card, then switch to **Admin view** to watch the bandit update.

Useful one-liners:

```bash
just demo-surface                         # trigger another demo surface
uv run kairos bookmarks prep --sync       # incremental X sync + enrich + research + embed + cluster
uv run kairos heartbeat                   # one direct policy cycle
uv run kairos optimize readiness          # check GEPA sample readiness
uv run kairos optimize nightly            # cron-safe prompt improvement pass
uv run kairos mcp                         # expose Kairos tools over MCP
```

After the server is running, open [http://127.0.0.1:8420/walkthrough](http://127.0.0.1:8420/walkthrough) for the animated prep -> heartbeat tour.

---

## System At A Glance

| Plane | What it does | Key files |
|-------|--------------|-----------|
| **Data plane** | X sync, bookmark enrichment, link research, embeddings, HDBSCAN clusters | `ingest/`, `bookmarks/`, `embeddings/` |
| **Context plane** | Calendar/Gmail/location/demo headspace, LLM moment narrative | `core/context.py`, `core/headspace.py`, `google/` |
| **Policy plane** | Vector match, Thompson sampling, interrupt gates, digest generation | `core/ranking.py`, `core/heartbeat.py`, `db/bandit.py` |
| **Learning plane** | Feedback events, alpha/beta updates, treatment posteriors, GEPA prompt diffs | `core/feedback.py`, `core/optimize.py`, `sim/` |
| **Surface plane** | FastAPI dashboard, SSE activity feed, MCP tools, optional OS delivery | `web/`, `delivery/`, `mcp/`, `agent/` |

The two important loops:

| Loop | Learns | Trigger | Artifact |
|------|--------|---------|----------|
| **Online bandit** | When/what to surface | Every feedback event | `bandit_params`, `bandit_treatments` |
| **Offline GEPA-style reflection** | How to phrase digests | `kairos optimize run/nightly` or `/api/optimize` | `optimization_runs` |

Read the full map in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Contributor Docs

| Start here | When you need |
|------------|---------------|
| [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) | UI/API contract, hotspots, extension checklist |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System diagrams and data model |
| [docs/TECH_DEBT.md](docs/TECH_DEBT.md) | What shipped, what to build next |
| [docs/MCP_SETUP.md](docs/MCP_SETUP.md) | Claude Code / Cursor MCP setup |
| [docs/GOOGLE_WORKSPACE_SETUP.md](docs/GOOGLE_WORKSPACE_SETUP.md) | Calendar/Gmail OAuth and headspace sync |
| [docs/LOCAL_QUEUE.md](docs/LOCAL_QUEUE.md) | Optional Redis + Arq prep queue |

---

## Where To Extend

If you want to contribute, pick one of these lanes.

| Lane | Good first expansion | Why it matters |
|------|----------------------|----------------|
| **Make learning visible** | Add a latest-learning trace panel from `pipeline_events` | Judges and users should see context -> rank -> feedback -> beta update in one glance |
| **Treatment lift** | Visualize `bandit_treatments` by digest style or prompt version | Connects GEPA wording changes to measured behavior |
| **Better context sharing** | Move from bucketed `context_class` to linear/contextual Thompson sampling | A click in one similar context should help another |
| **Sleep-time cache** | Precompute candidate digests after Google sync or prep jobs | Keeps heartbeat latency low without weakening intelligence |
| **More sources** | Readwise, Pocket, browser export, RSS | Tests whether the policy is source-agnostic |
| **Trace infrastructure** | Add `decision_id`, prompt version, model input/output, latency, reward | Turns the self-improvement stack into a research-quality dataset |

The strongest product rule: if it does not improve the interruption policy or make learning legible, defer it.

---

## Recommended Reading

### Core Thesis: Learning When To Interrupt

- [PLAN.md](PLAN.md) - product thesis and roadmap.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - how the heartbeat, ranking, and feedback loop fit together.
- [Contextual bandits / LinUCB](https://arxiv.org/abs/1003.0146) - the classic shape behind context-aware recommendation.
- [Action-Centered Thompson Sampling](https://proceedings.neurips.cc/paper_files/paper/2017/hash/4f6ffe13a5d75b2d6a3923922b3922e5-Abstract.html) - useful mental model for interventions and feedback.

### Self-Improvement And Prompt Optimization

- [docs/TECH_DEBT.md](docs/TECH_DEBT.md) - current "what next" list for making the stack stronger.
- [GEPA paper](https://arxiv.org/abs/2507.19457) - prompt/program evolution from feedback signals.
- [DSPy GEPA overview](https://dspy.ai/api/optimizers/GEPA/overview/) - production-oriented optimizer API inspiration.

### Agent Infrastructure And Observability

- [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) - practical guide to API endpoints, SSE events, and fragile assumptions.
- [docs/MCP_SETUP.md](docs/MCP_SETUP.md) - MCP tool path for Claude Code and Cursor.
- [docs/GOOGLE_WORKSPACE_SETUP.md](docs/GOOGLE_WORKSPACE_SETUP.md) - Google OAuth, Calendar/Gmail sync, and ADK path.
- [OpenInference](https://github.com/Arize-ai/openinference) - useful reference point for future LLM trace export.

### Bookmark Intelligence And Data Plane

- [Turso / libSQL vector search](https://docs.turso.tech/features/ai-and-embeddings) - cluster and bookmark ranking backend.
- [HDBSCAN documentation](https://hdbscan.readthedocs.io/) - density clustering for noisy bookmark corpora.
- [Google Gemini API docs](https://ai.google.dev/gemini-api/docs) - Gemini generation and embeddings reference.
- [X API bookmarks endpoint](https://docs.x.com/x-api/users/get-bookmarks) - primary ingest path.

---

## Rewarding Rabbit Holes

These are deliberately scoped research threads that map back to code in this repo.

1. **Treatment-aware bandits.** Kairos already writes `bandit_treatments`. The next step is showing whether digest style or prompt version changes engagement.
2. **Linear Thompson sampling.** Replace sparse context buckets with feature vectors: gap length, meeting density, topic affinity, hour, surface fatigue.
3. **Delayed feedback.** A user might click later or ignore silently. Explore decay windows, censored rewards, and off-policy evaluation.
4. **Sleep-time compute.** Precompute cluster dossiers and digest drafts while idle, then keep heartbeat fast.
5. **Cohort priors.** Cold-start new users from similar users without losing per-user personalization.
6. **Trace joins.** Add a durable `decision_id` through heartbeat -> LLM call -> notification -> feedback. This is the substrate for serious prompt optimization.
7. **Multi-source memory.** Add non-X sources and test whether the policy still works when content shape changes.

For a curated list of post-demo bets, see [docs/TECH_DEBT.md](docs/TECH_DEBT.md) and [docs/archive/research/CURSOR.md](docs/archive/research/CURSOR.md).

---

## CLI Cheat Sheet

```bash
# Data plane
uv run kairos x auth
uv run kairos bookmarks prep --sync
uv run kairos bookmarks clusters

# Policy plane
uv run kairos heartbeat
uv run kairos heartbeat --via-agent
uv run kairos feedback <notification-id> dismissed

# Self-improvement
uv run kairos sim run --days 14 --personas alex,maya,jordan
uv run kairos optimize readiness
uv run kairos optimize run --dry-run
uv run kairos optimize nightly

# Interfaces
uv run kairos serve
uv run kairos mcp
uv run kairos worker
```

---

## Project Stance

Kairos is not trying to be a general second brain, a search box, or a notification firehose.

It is a small, inspectable system for one claim:

> Saved knowledge becomes useful when the agent learns the right moment to bring it back.
