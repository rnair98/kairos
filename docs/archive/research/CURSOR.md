# Cursor Research Session Transcript

Session captured from the Kairos architecture research discussion in Cursor (June 2026).

**Starting prompt:**

> `/plugin-10x-swe-exa/web_search_help` in our current architecture, is there any mathematical/cs/ai research that if implemented, would be a force-multiplier for our usecase?

---

## Context: Kairos architecture (at time of discussion)

Kairos is a **contextual bandit** that learns **when** to surface bookmark *clusters* — not a search engine, not a cron digest. Silence (`KAIROS_OK`) is the default.

**Core loop:**

```text
Entry (CLI / web / MCP / ADK agent)
  → HeartbeatService.run(user_id)
  → refresh_fatigue_fields → prepare_context_for_decision (LLM narrative, TTL)
  → evaluate_surface (vector + per-user bandit + hard gates + moment_fit + multi-step digest)
  → SURFACE → apply_surface_fatigue → deliver
```

**Hybrid split:**

- **ADK** — agent orchestration, Workspace MCP toolsets
- **google-genai Interactions** — intelligence layer, digest, enrichment, cluster labels
- **Thompson sampling** — per-user × cluster × context_class bandit (deterministic gates stay separate)

**Already shipped before this session (cleanup pass):**

- Per-user bandit and snooze
- Atlas `$vectorSearch` with in-memory fallback
- Early hard-gate skip before vector encode
- Evergreen clusters skip Google Search grounding
- Parallel LLM cluster labeling
- `embedding_backend` default reverted to `gemini` (Cloud Run — not local sentence-transformers)
- Dashboard mock cleanup; live metrics from `/api/metrics`

---

## Part 1: Force-multiplier research (bandit + decision theory)

Web search (Exa) across contextual bandits, interruptibility, proactive AI, PKM, and LLM+bandit literature.

### Tier 1 — Highest force-multiplier (fits core loop)

#### 1. Non-myopic / long-horizon notification RL

**Papers:**

- [O'Brien et al. 2022 — Should I send this notification?](https://arxiv.org/pdf/2202.08812) (Meta)
- [Steyvers & Mayer 2025 — When not to help](https://arxiv.org/pdf/2508.01837) (POMDP for engagement)

**Why:** Thompson sampling optimizes this tick's reward. Real failure mode is tomorrow's disengagement (too many surfaces → ignore/snooze/disable).

**Kairos fit:** Model latent **receptivity**; choose SURFACE vs KAIROS_OK to maximize 7-day engagement. Refine or replace hand-tuned `daily_surface_budget` + `min_gap`.

**Where:** `core/ranking.py` gate layer + new `core/ltv.py`.

---

#### 2. Generator-mediated bandits (GAMBITTS)

**Paper:** [Generator-Mediated Bandits (May 2025)](https://arxiv.org/html/2505.16311)

**Why:** Action (which cluster) is split from treatment (LLM digest). Standard bandit only learns from `cluster_id`; GAMBITTS learns from observed digest too.

**Kairos fit:** After `generate_cluster_digest`, embed digest → update posteriors for (cluster, digest-style).

**Where:** `db/bandit.py` — extend key or treatment bucket.

---

#### 3. Holistic intra-day scheduling (TIM)

**Paper:** [TIM — Temporal Interaction Model (Kuaishou, 2024)](https://arxiv.org/pdf/2406.07067)

**Why:** `daily_surface_budget=3` but no *when within the day* optimization. TIM predicts slot-wise CTR and schedules multiple notifications holistically.

**Kairos fit:** Heartbeat picks best cluster *now*; TIM layer decides whether this slot is worth spending budget on.

**Where:** New `core/scheduler.py` before `evaluate_surface`.

---

#### 4. Delayed / partially observed feedback

**Paper:** [Bootstrap Thompson for delayed rewards (UAI 2024)](https://proceedings.mlr.press/v244/gigli24a.html)

**Why:** `link_click` / `acted` arrive minutes later; instant α/β updates add noise.

**Kairos fit:** Censored/delayed reward buffer; apply bandit update when window closes.

**Where:** `core/feedback.py`, `db/bandit.py`.

---

### Tier 2 — Strong upgrades (moderate engineering)

| Research | Benefit | Kairos fit |
|----------|---------|------------|
| **CBLI** — [EMNLP 2024](https://aclanthology.org/2024.emnlp-main.1107/) | Cold-start priors from LLM-simulated preferences | Seed α,β at cluster/first-login; conservative priors |
| **PRISM** — [Feb 2026](https://arxiv.org/html/2602.01532) | Calibrated speak-vs-silent under asymmetric costs | Replace binary `moment_fit` threshold |
| **VITS** — [ICML 2024](https://proceedings.mlr.press/v235/clavier24a.html) | Neural contextual bandit over moment embedding | Generalize across context classes |
| **Graph spreading activation** — SYNAPSE, PersonalAI | Cluster relevance beyond centroid cosine | `adjusted = (w1·cosine + w2·graph) × bandit` |

---

### Tier 3 — Adjacent (use carefully)

| Research | Verdict |
|----------|---------|
| Spaced repetition / FSRS | Optimizes *recall*, not *interrupt timing* — subordinate feature only |
| Dueling bandits / RLHF | Useful when only pairwise labels exist |
| Full PKM graphs (MNEME, Zettelkasten) | Big infra; thesis is bandit policy |
| Optimal stopping (pure) | Useful for *when to stop waiting*, less for *which cluster* |

---

### Recommended bandit research order

```text
1. Delayed feedback TS     — low risk, fixes statistical bug
2. GAMBITTS digest learning — uses digest already generated
3. TIM daily scheduler      — multiplies budget gate value
4. LTV / receptivity POMDP  — biggest moat for "learns when to interrupt"
5. CBLI priors              — demo + new-user experience
6. Graph activation ranker  — ranking quality without policy change
```

---

## Part 2: Sleep-time Compute paper

**User question:** How about [Sleep-time Compute (arXiv:2504.13171)](https://arxiv.org/abs/2504.13171)?

**Paper:** Lin et al. (Berkeley / Letta). Decompose interaction into context `c` (available early) and query `q` (at test-time):

```text
Sleep-time:  S(c) → c′     (heavy reasoning while idle)
Test-time:   T_b(q, c′) → a   (small budget, b ≪ B)
```

**Results:** ~5× less test-time compute for same accuracy; ~2.5× lower cost per query when many queries share one context. Uplift largest when **q is predictable from c**.

### Mapping to Kairos

| Paper concept | Kairos today | Gap |
|---------------|--------------|-----|
| Context `c` available early | Calendar/Gmail sync, `context_cache`, clusters | ✅ |
| Query `q` at test-time | Each heartbeat: rank + gate + digest | ✅ |
| `S(c) → c′` offline | `moment_narrative` + TTL | Partial |
| Amortize across many `qᵢ` | Heartbeat every 5 min, same headspace | Underused |
| Predictability gate | Not explicit | Missing |

**Verdict:** High force-multiplier for **latency/cost**, not bandit learning. Complements Thompson sampling — bandit decides SURFACE vs KAIROS_OK; sleep-time makes SURFACE fast.

**Implementation sketch:**

```text
core/sleep_cache.py
  build_surface_cache(user_id, context) → SurfaceCache
    top_clusters, digest_drafts, moment_fit hints
    fingerprint, expires_at

Trigger S(c) on: headspace sync, /api/context/fuse, cron — NOT every heartbeat
Invalidate on: calendar change, fatigue/snooze delta, fingerprint mismatch
```

**Prior art in repo:** PLAN.md references Letta sleep-time; FAQ distinguishes Letta memory vs Kairos policy; GEPA UI panel labeled "SLEEP-TIME PASS · 02:00" (P7, not yet shipped).

---

## Part 3: Letta research — what helps Kairos?

**User question:** Are there other things from [Letta](https://www.letta.com) that would help us?

**Thesis line (from FAQ):** Letta optimizes *what is stored*; Kairos optimizes *when* saved knowledge interrupts you.

### Borrow vs skip

| Letta idea | Borrow? | Notes |
|------------|---------|-------|
| [Sleep-time compute](https://www.letta.com/blog/sleep-time-compute/) | **Yes** | Pre-materialize digests / rankings |
| [Primary + sleep-time dual agent](https://docs.letta.com/guides/agents/architectures/sleeptime) | **Yes** | Fast heartbeat + async heavy worker |
| [Memory blocks](https://www.letta.com/blog/memory-blocks) | **Partial** | Structured context compilation |
| [Context Repositories / MemFS](https://www.letta.com/blog/context-repositories/) | **Partial** | Git-version GEPA prompts (P7) |
| [Context Constitution](https://github.com/letta-ai/context-constitution) | **Partial** | Prompt discipline for ADK harness |
| [Memory Models / meta-RL](https://www.letta.com/blog/towards-agents-that-learn/) | **No (now)** | Different product |
| Full MemGPT memory OS | **No** | Bookmarks from X + Mongo |

### Letta patterns worth adopting

1. **Dual-agent split** — ADK/heartbeat (fast) vs sleep worker (post-sync cron, slow model)
2. **Memory blocks → context compiler** — fixed slots, char caps: headspace, fatigue, learned_context, policy_hints
3. **Context Repositories → GEPA** — git-versioned digest prompts + `optimization_runs`
4. **Sleep frequency** — trigger on context fingerprint change, not every tick (Letta default: every N steps)
5. **Memory defrag → index hygiene** — periodic re-cluster, refresh labels, prune stale embeds
6. **Context-Bench shape → eval harness** — long-horizon gym + trace joins for GEPA

### Do not adopt

- Experiential agent identity / self-editing system prompt
- Agent-written archival memory
- Letta Agent SDK alongside Kairos
- Memory Models (meta-RL for curation)

---

## Part 4: Final verdict — what to implement

**User question:** So what's the final verdict of what research to implement and integrate?

### Three research lines (compose cleanly)

```text
LINE A: Policy learning (bandit research)     — WHEN to surface (online)
LINE B: Sleep-time compute (Letta / Lin 2025) — HOW FAST/CHEAP surface path (offline)
LINE C: Prompt RSI (GEPA + traces)            — HOW WELL digests read (offline, P7)
```

### Already integrated

- Per-user Thompson bandit
- Early hard-gate skip
- Atlas `$vectorSearch` + fallback
- Narrative TTL (partial sleep-time)
- Evergreen → skip search grounding
- Per-user snooze
- Parallel cluster labeling
- Sim gym eval infra
- `embedding_backend=gemini` default (Cloud Run)

### Ship next (ordered by ROI)

| Phase | What | Research source | Priority |
|-------|------|-----------------|----------|
| **1** | Sleep cache + dual-agent sleep worker | [Sleep-time Compute](https://arxiv.org/abs/2504.13171), [Letta sleep-time agents](https://docs.letta.com/guides/agents/architectures/sleeptime) | **#1** |
| **2** | Delayed-feedback bandit updates | [Bootstrap TS, UAI 2024](https://proceedings.mlr.press/v244/gigli24a.html) | **#2** |
| **3** | GAMBITTS-lite (digest treatment in bandit) | [GAMBITTS](https://arxiv.org/html/2505.16311) | **#3** |
| **4** | `llm_traces` + GEPA + git-versioned prompts | PLAN Phase D, Letta Context Repos | **#4 (P7)** |
| **5** | Memory-block context compiler | [Letta memory blocks](https://www.letta.com/blog/memory-blocks) | Polish |

### Post-demo / if traction

| Research | Verdict |
|----------|---------|
| TIM slot-wise scheduling | Implement |
| LTV / POMDP receptivity | Implement — biggest research moat |
| CBLI LLM priors | Implement lightly |
| Graph spreading activation | Consider |
| PRISM calibrated speak/silent | Consider |
| VITS / neural contextual bandit | Defer until more feedback data |

### Explicit skip list

1. Agent-written archival memory (MemGPT)
2. Memory Models + meta-RL ([Letta 2026](https://www.letta.com/blog/towards-agents-that-learn/))
3. Experiential agent identity
4. Spaced repetition as core ranking signal
5. Neural bandits before thousands of feedback events
6. Full Letta Agent integration

### Roadmap Sequence

```text
NOW     Phase 1  Sleep cache + dual-agent sleep worker
NEXT    Phase 2  Delayed-feedback bandit updates
THEN    Phase 3  GAMBITTS-lite
P7      Phase 4  llm_traces + GEPA + git-versioned prompts
POLISH  Phase 5  Memory-block context compiler

LATER   TIM → LTV/POMDP → CBLI → graph ranker
NEVER   MemGPT OS, memory models, spaced-rep core, Letta SDK
```

### One-sentence pitch

> Kairos learns **when** to interrupt via an online contextual bandit; **sleep-time compute** pre-materializes expensive intelligence so heartbeats stay fast; **GEPA** improves digest quality offline from the same feedback — three loops, no weight training, no memory OS.

### Recommended single implementation bundle

**Phase 1 + Phase 2** — sleep-time for speed, delayed feedback for learning quality; both aligned with thesis, shippable without rewriting ranking.

---

## Related docs in repo

- [ARCHITECTURE.md](../../ARCHITECTURE.md) — system map
- [PLAN.md](../../../PLAN.md) — roadmap, GEPA, two-loop design
- [CLOUD_RUN.md](../../CLOUD_RUN.md) — deploy, vector search, slim image
- [AGENTS.md](../../../AGENTS.md) — workspace facts for agents

## Key external references

- Sleep-time Compute: https://arxiv.org/abs/2504.13171
- Letta research: https://www.letta.com/research
- Letta sleep-time blog: https://www.letta.com/blog/sleep-time-compute
- GAMBITTS: https://arxiv.org/html/2505.16311
- Generator-Mediated Bandits / Thompson for GenAI interventions (May 2025)
- CBLI (EMNLP 2024): https://aclanthology.org/2024.emnlp-main.1107/
- Context Constitution: https://github.com/letta-ai/context-constitution

---

*Generated from Cursor agent session. Not a live transcript — synthesized from discussion turns for team reference.*
