# Kairos demo runbook

Single entry point for stage rehearsal (~3 min). Browser-first; MCP optional.

---

## Quick start

```bash
# One terminal — prep, surface, dashboard
just demo-serve

# Re-surface without re-prep
just demo-surface
```

| Command | Does |
|---------|------|
| `just demo-serve` | Prep → dashboard → surface (blocks) |
| `just demo` / `just demo-launch` | Alias for `demo-serve` |
| `just demo-surface` | Re-surface only (dashboard already up) |
| `just demo-prep` | Corpus + gym + headspace only |
| `just demo-help` | All commands + env skips |

**Env skips:** `SKIP_CORPUS=1` · `SKIP_GYM=1` · `SKIP_GOOGLE=1` · `DEMO_RESEARCH_LIMIT=20`

Demo env loads from `.env.demo` (fast digests, manual surface, lower score threshold).

---

## Product pipeline (what judges should see)

1. **Ingest** — X bookmarks in MongoDB  
2. **Enrich** — tags, consumption mode, energy cost (`enrich.py`)  
3. **Research** — grounded summary, relevance signal, ✓/●/✗ chip, sources (`kairos bookmarks research`)  
4. **Cluster** — HDBSCAN topic groups in sidebar  
5. **Surface** — digest with cluster summary, why-now, pre-validated links  
6. **Learn** — dismiss → bandit β ↑; gym sparkline in Admin  

Prep runs enrich + research via `just demo-serve` (or `just demo-prep` alone). Research **fetches each linked article** (follows t.co → HTML), then Gemini synthesizes a summary with Google Search validation.

---

## 30-second pitch

> Everyone embeds bookmarks. Nobody reads them — because they saved them in one headspace and try to retrieve in another.
>
> Kairos is a **contextual bandit** that learns *when* to surface a topic cluster based on calendar gaps, location, and engagement history.
>
> **Silence is the default.** `KAIROS_OK` means "not now." The policy updates from every snooze and dismiss.

**Close line:** "Everyone embeds bookmarks. Kairos learns *when* to interrupt — and gets quieter when you dismiss at the wrong moment."

### Tough questions

| Question | Answer |
|----------|--------|
| "Isn't this just RAG?" | RAG answers *what*. Kairos learns *when* — interrupt policy, not search. |
| "Why clusters?" | Bookmarks arrive one-at-a-time; topics arrive as moments. Digests, not single links. |
| "Only 2 clusters?" | Small corpus + HDBSCAN — one mega-cluster is the long tail; mechanism works as corpus grows. |
| "Demo headspace?" | Cafe + 90m gap by default; live calendar via `kairos google connect` + sync. |
| "GEPA / sleep-time?" | Bandit = online timing (shipped). GEPA = offline prompt improvement (CLI + API + fixtures + cron-safe `optimize nightly` shipped). |

More depth: [FAQ.md](./FAQ.md)

---

## Three-act script (~3 min)

**Persona:** Alex — senior engineer, hundreds of bookmarks, read ~5%.

### Act 1 — Graveyard → intelligence (45s)

**View:** User · **Prep:** `just demo-serve` (once)

> "Everything Alex saved — clustered, enriched, researched. Two years of intent."

- Sidebar: cluster names + bookmark counts  
- Surface card (when ready): link **summaries**, validation chips, "checked" sources — not raw tweet URLs  
- If amber banner: demo headspace stub; live calendar is optional  

> "Silence is the default. Kairos asks: *is this the right moment?* — not *what did I save?*"

### Act 2 — Right moment (90s)

**Trigger:** Admin → **Surface now** or `just demo-surface`

**View:** Admin first (pipeline log), then User (digest)

- Pipeline: context → rank → gates → digest (draft → critique → revise) → deliver  
- User card: cluster summary, **why now**, researched links  
- Click **Not relevant**  
- Admin: **learn ›** feedback line, bandit **β ↑**  

> "One dismiss. Online bandit update — no gradient steps on Gemini."

Optional second surface: different cluster or `KAIROS_OK` (fatigue gate) — both valid.

### Act 3 — Learning (45s)

**View:** Admin

- **Engagement trend** sparkline (from persona gym in prep)  
- **Bandit learning** panel — α, β, P(engage)  
- GEPA panel: show a pre-seeded `optimization_runs` diff if possible; `POST /api/optimize` or `kairos optimize run --dry-run` is available, but don't depend on live Gemini latency on stage  

> "The gym is how we know the policy converges before we deploy it."

---

## Presenter cheat sheet

| Moment | View | Action |
|--------|------|--------|
| Opening — corpus | User | Sidebar clusters + counts |
| Policy trace | Admin | Pipeline log (live narration) |
| Surface | Admin / User | **Surface now** or `just demo-surface` |
| Dismiss | User | **Not relevant** |
| Bandit update | Admin | Bandit learning (β ↑) |
| Gym curve | Admin | Engagement trend |

Toggle: **Admin view** / **← User view** (top-right).

---

## Timing

| Act | Target | Max |
|-----|--------|-----|
| Act 1 | 40s | 50s |
| Act 2 | 85s | 100s |
| Act 3 | 40s | 50s |
| **Total** | **~3:00** | **3:20** |

---

## Readiness checklist

| Beat | Ready? | How |
|------|--------|-----|
| Enriched corpus | ✅ | `just demo-prep` → enrich |
| Researched links | ✅ | `just demo-prep` → research |
| Clusters in sidebar | ✅ | MongoDB + `/api/clusters` |
| Surface digest | ✅ | `just demo-surface` or **Surface now** |
| Dismiss → bandit | ✅ | User dismiss → Admin β |
| Engagement sparkline | ✅ | gym seed in prep (`SKIP_GYM=1` to skip) |
| MCP agent path | ✅ | § MCP below + [MCP_SETUP.md](../MCP_SETUP.md) |
| Live Google calendar | ⚠️ optional | `kairos google connect` + sync |

**Still optional (cut if behind):** live X sync on stage · OS notifications · `/loop` rehearsed once

---

## MCP / agent path (optional)

Two paths for live Calendar/Gmail:

| Path | When | Flow |
|------|------|------|
| **Kairos MCP** | Claude Code `/loop` | `sync_google_headspace` → `run_heartbeat` |
| **ADK agent** | Workspace MCP sensor fusion | Calendar/Gmail MCP → `fuse_headspace_context` → `run_heartbeat` (`kairos agent-cycle`) |

Setup: [MCP_SETUP.md](../MCP_SETUP.md). Do **not** call `sync_google_headspace` on the ADK agent path — use Workspace MCP + fuse instead.

**Prerequisites:** MongoDB, `GEMINI_API_KEY`, MCP configured, optional `kairos google connect` → `KAIROS_USER_ID`.

**Demo-fast MCP env:**

```json
"GROUNDING_PROVIDER": "exa",
"DIGEST_USE_WEB_GROUNDING": "true",
"DIGEST_USE_GOOGLE_SEARCH": "false",
"INTELLIGENCE_MOMENT_FIT_CHECK": "false",
"SURFACE_SCORE_THRESHOLD": "0.08"
```

**Parallel browser:**

```bash
just demo-serve           # prep + dashboard + surface
just demo-surface         # re-trigger only
```

**One-shot in chat:**

```
1. sync_google_headspace(user_id="<KAIROS_USER_ID>")
2. run_heartbeat(delivery="return_only")
3. If SURFACE: show delivery.rendered_markdown
4. record_feedback(notification_id="<id>", action="dismissed")
5. run_heartbeat again — cite gate / bandit change
```

**`/loop` prompt (Claude Code):**

```
/loop 5m

Each cycle:
1. sync_google_headspace() if calendar changed (skip after first tick)
2. run_heartbeat(delivery="return_only")
3. SURFACE → one-sentence summary; KAIROS_OK → cite reason
4. record_feedback only when I explicitly dismiss/snooze
```

MCP and browser share MongoDB — bandit updates appear in Admin either way.

---

## Risks & recovery

| Risk | Mitigation |
|------|------------|
| Digest slow (10–25s) | Keep Exa grounding enabled for the demo; rely on prep/link research and pre-warmed corpus. For offline rehearsal only, set `GROUNDING_PROVIDER=none`. |
| `KAIROS_OK` on stage | **Surface now** / `just demo-surface` (atomic reset + heartbeat); close stale browser tabs |
| Moment-fit blocks surface | `INTELLIGENCE_MOMENT_FIT_CHECK=false` in `.env.demo` |
| Raw X URLs only | Run prep without `SKIP_CORPUS=1` |
| Bandit β invisible | Admin → Bandit learning panel |
| SSE quiet after CLI heartbeat | Events persist to Mongo — refresh admin log; or use **Surface now** (server-side) |
| Dashboard won't start | CLI: `kairos heartbeat` → `kairos feedback` |

---

## Internals (Just recipes)

All demo tasks live in the root `Justfile`: `demo-serve` · `demo-prep` · `demo-corpus` · `demo-surface` · `demo-reset` · `demo-seed-gym` · `demo-sync-google`

Run `just --list` or `just demo-help` — you rarely need individual recipes directly.
