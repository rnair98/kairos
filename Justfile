# Kairos task runner — https://github.com/casey/just
# Demo runbook: docs/demo-readiness/DEMO.md

set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

demo-port := env_var_or_default("DEMO_PORT", "8420")
demo-base := "http://127.0.0.1:" + demo-port

default:
    @just --list --unsorted

# Remove generated local artifacts. Keeps .venv, env files, and app data intact.
clean:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "▸ Cleaning generated artifacts…"
    find . \
      -path './.git' -prune -o \
      -path './.venv' -prune -o \
      -type d \( \
        -name '__pycache__' -o \
        -name '.pytest_cache' -o \
        -name '.ruff_cache' -o \
        -name '.mypy_cache' -o \
        -name 'htmlcov' -o \
        -name 'build' -o \
        -name 'dist' -o \
        -name '*.egg-info' \
      \) -prune -exec rm -rf {} +
    find . \
      -path './.git' -prune -o \
      -path './.venv' -prune -o \
      -type f \( \
        -name '*.pyc' -o \
        -name '*.pyo' -o \
        -name '.coverage' -o \
        -name '.coverage.*' -o \
        -name '.dmypy.json' -o \
        -name 'dmypy.json' \
      \) -delete
    echo "  ✓ clean"

# Prep + surface + dashboard (one terminal)
demo:
    @just demo-serve

# Alias
demo-launch override="":
    @just demo-serve override='{{override}}'

# Prep + surface + dashboard — start here for stage rehearsal
demo-serve port=demo-port override="":
    #!/usr/bin/env bash
    set -euo pipefail
    [[ -f .env.demo ]] && set -a && source .env.demo && set +a
    BASE="http://127.0.0.1:{{port}}"

    just demo-prep
    echo ""

    SERVER_PID=""
    cleanup() { [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true; }
    trap cleanup EXIT INT TERM

    if curl -sf "${BASE}/api/config" >/dev/null 2>&1; then
      echo "▸ Dashboard already running at ${BASE}"
    else
      echo "▸ Starting dashboard on ${BASE}…"
      just _demo-serve-app {{port}} &
      SERVER_PID=$!
      for _ in $(seq 1 60); do
        curl -sf "${BASE}/api/config" >/dev/null 2>&1 && break
        sleep 0.5
      done
      curl -sf "${BASE}/api/config" >/dev/null 2>&1 || { echo "✗ Server not ready" >&2; exit 1; }
      echo "  ✓ dashboard ready"
    fi

    echo ""
    just demo-surface override='{{override}}' || true
    echo ""
    echo "▸ Open ${BASE} — dismiss → Admin → bandit β"
    if [[ -n "$SERVER_PID" ]]; then
      echo "  Ctrl+C stops the dashboard"
      wait "$SERVER_PID"
    fi

# One-shot prep: corpus + gym + headspace (+ optional Google)
demo-prep:
    #!/usr/bin/env bash
    set -euo pipefail
    [[ -f .env.demo ]] && set -a && source .env.demo && set +a
    echo "══ Kairos demo prep ══"
    echo ""
    if [[ "${SKIP_CORPUS:-}" != "1" ]]; then
      echo "▸ Preparing bookmark corpus (enrich + research)…"
      just demo-corpus || echo "  ⚠ corpus prep incomplete — run: just demo-corpus"
      echo ""
    fi
    if [[ "${SKIP_GYM:-}" != "1" ]]; then
      echo "▸ Seeding engagement curve (persona gym)…"
      if just demo-seed-gym 7 demo 2>/dev/null; then
        echo "  ✓ gym seeded"
      else
        echo "  ⚠ gym skipped (needs MongoDB + clusters)"
      fi
      echo ""
    fi
    echo "▸ Resetting demo headspace…"
    just demo-reset
    echo ""
    if [[ "${SKIP_GOOGLE:-}" != "1" ]] && [[ -n "${KAIROS_USER_ID:-}" || -n "${GOOGLE_REFRESH_TOKEN:-}" ]]; then
      echo "▸ Syncing Google headspace…"
      if just demo-sync-google 2>/dev/null; then
        echo "  ✓ live calendar context"
      else
        echo "  ⚠ google sync failed — demo stub OK on stage"
      fi
      echo ""
    elif [[ "${SKIP_GOOGLE:-}" != "1" ]]; then
      echo "▸ Google sync skipped — run: uv run kairos google connect"
      echo ""
    fi
    echo "▸ Ready: just demo-serve  |  just demo-surface"
    echo "  Docs: docs/demo-readiness/DEMO.md"

# Enrich + research + embed + cluster (single pipeline)
demo-corpus:
    #!/usr/bin/env bash
    set -euo pipefail
    [[ -f .env.demo ]] && set -a && source .env.demo && set +a
    echo "▸ Bookmark prep (enrich → research → embed → cluster)…"
    PREP_CMD=(uv run kairos bookmarks prep)
    [[ "${SKIP_ENRICH:-}" == "1" ]] && PREP_CMD+=(--skip-enrich)
    [[ "${SKIP_RESEARCH:-}" == "1" ]] && PREP_CMD+=(--skip-research)
    [[ "${SKIP_EMBED:-}" == "1" ]] && PREP_CMD+=(--skip-embed)
    [[ "${SKIP_CLUSTER:-}" == "1" ]] && PREP_CMD+=(--skip-cluster)
    [[ -n "${DEMO_RESEARCH_LIMIT:-}" ]] && PREP_CMD+=(--research-limit "$DEMO_RESEARCH_LIMIT")
    [[ -n "${RESEARCH_CONCURRENCY:-}" ]] && PREP_CMD+=(--research-concurrency "$RESEARCH_CONCURRENCY")
    [[ "${RESEARCH_CLUSTERED_ONLY:-}" == "1" ]] && PREP_CMD+=(--clustered-only)
    if "${PREP_CMD[@]}"; then
      echo "▸ Corpus ready"
    else
      echo "▸ Prep had failures — re-run or check partial progress" >&2
      exit 1
    fi

# Reset fatigue gates for reliable SURFACE
demo-reset:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "▸ Resetting demo headspace (90m gap, cafe + architecture review)…"
    uv run python - <<'PY'
    import asyncio
    from kairos.core.demo import reset_demo_headspace
    asyncio.run(reset_demo_headspace())
    print("  ✓ context reset — gates open for next heartbeat")
    PY

# Persona gym sparkline (Act 3)
demo-seed-gym days="7" run_id="demo":
    @echo "▸ Running persona gym (alex, {{days}} days, run_id={{run_id}})…"
    uv run kairos sim run --days {{days}} --personas alex --run-id {{run_id}}
    @echo "▸ Reset sim data: uv run kairos sim reset --run-id {{run_id}}"

# Live calendar context
demo-sync-google *args:
    #!/usr/bin/env bash
    set -euo pipefail
    [[ -f .env.demo ]] && set -a && source .env.demo && set +a
    echo "▸ Syncing Google headspace…"
    [[ -z "${KAIROS_USER_ID:-}" ]] && echo "  Tip: set KAIROS_USER_ID after kairos google connect"
    exec uv run kairos google sync ${KAIROS_USER_ID:+--user-id "$KAIROS_USER_ID"} "$@"

# Uvicorn only — used internally by demo-serve
[private]
_demo-serve-app port:
    #!/usr/bin/env bash
    set -euo pipefail
    [[ -f .env.demo ]] && set -a && source .env.demo && set +a
    export GROUNDING_PROVIDER="${GROUNDING_PROVIDER:-exa}"
    export DIGEST_USE_WEB_GROUNDING="${DIGEST_USE_WEB_GROUNDING:-true}"
    export DIGEST_USE_GOOGLE_SEARCH="${DIGEST_USE_GOOGLE_SEARCH:-false}"
    export INTELLIGENCE_DIGEST_RUNTIME_FAST="${INTELLIGENCE_DIGEST_RUNTIME_FAST:-true}"
    export INTELLIGENCE_MOMENT_FIT_CHECK="${INTELLIGENCE_MOMENT_FIT_CHECK:-false}"
    export DEMO_MODE="${DEMO_MODE:-true}"
    export AUTO_HEARTBEAT="${AUTO_HEARTBEAT:-false}"
    export DECISION_INTERVAL_SECONDS="${DECISION_INTERVAL_SECONDS:-120}"
    exec uv run kairos serve --host 127.0.0.1 --port {{port}}

# Reset + surface (re-trigger when dashboard is already running)
demo-surface override="":
    #!/usr/bin/env bash
    set -euo pipefail
    [[ -f .env.demo ]] && set -a && source .env.demo && set +a
    PORT="{{demo-port}}"
    BASE="{{demo-base}}"
    OVERRIDE="{{override}}"
    body='{}'
    if [[ -n "$OVERRIDE" ]]; then
      body=$(OVERRIDE="$OVERRIDE" uv run python -c "import orjson, os; print(orjson.dumps({'context_override': os.environ['OVERRIDE']}).decode())")
    fi
    if curl -sf "${BASE}/api/config" >/dev/null 2>&1; then
      echo "▸ Demo surface via dashboard API…"
      SURFACE_TMP=$(mktemp)
      HTTP_CODE=$(curl -sS --max-time 300 -o "$SURFACE_TMP" -w "%{http_code}" \
        -X POST "${BASE}/api/demo/surface" -H 'Content-Type: application/json' -d "$body") || HTTP_CODE="000"
      resp=$(cat "$SURFACE_TMP")
      rm -f "$SURFACE_TMP"
      if [[ "$HTTP_CODE" != "200" ]]; then
        echo "  ✗ surface HTTP ${HTTP_CODE}" >&2
        echo "${resp:0:800}" >&2
        exit 1
      fi
    else
      echo "▸ Dashboard not running — reset + CLI heartbeat…"
      just demo-reset
      export GROUNDING_PROVIDER="${GROUNDING_PROVIDER:-exa}"
      export DIGEST_USE_WEB_GROUNDING="${DIGEST_USE_WEB_GROUNDING:-true}"
      export DIGEST_USE_GOOGLE_SEARCH="${DIGEST_USE_GOOGLE_SEARCH:-false}"
      export INTELLIGENCE_DIGEST_RUNTIME_FAST="${INTELLIGENCE_DIGEST_RUNTIME_FAST:-true}"
      export INTELLIGENCE_MOMENT_FIT_CHECK="${INTELLIGENCE_MOMENT_FIT_CHECK:-false}"
      if [[ -n "$OVERRIDE" ]]; then
        resp=$(uv run kairos heartbeat --context-override "$OVERRIDE" 2>/dev/null)
      else
        resp=$(uv run kairos heartbeat 2>/dev/null)
      fi
    fi
    echo "$resp" | uv run python - <<'PY'
    import orjson, sys
    raw = sys.stdin.read()
    try:
        o = orjson.loads(raw)
    except orjson.JSONDecodeError:
        print(raw[-1500:])
        raise SystemExit(1)
    status = o.get("status")
    reason = o.get("reason")
    n = o.get("notification") or {}
    d = n.get("digest") or {}
    print(f"  → {status}", end="")
    if d.get("cluster_name"):
        print(f" · {d['cluster_name']}", end="")
    print()
    if reason:
        print(f"  reason: {reason}")
    if n.get("notification_id"):
        print(f"  notification: {n['notification_id']}")
    for line in o.get("activity") or []:
        if "gate" in line or "surfaced" in line.lower():
            print(f"  {line}")
    PY

# Alias
demo-heartbeat override="":
    @just demo-surface override='{{override}}'

# Print demo help + runbook pointer
demo-help:
    @echo "Kairos demo — docs/demo-readiness/DEMO.md"
    @echo ""
    @echo "  just demo-serve         prep + surface + dashboard (start here)"
    @echo "  just demo               alias for demo-serve"
    @echo "  just demo-launch        alias for demo-serve"
    @echo "  just demo-prep          corpus + gym + headspace only"
    @echo "  just demo-surface       re-surface (dashboard already up)"
    @echo ""
    @echo "  SKIP_CORPUS=1  SKIP_GYM=1  SKIP_GOOGLE=1  DEMO_RESEARCH_LIMIT=N"
    @echo "  RESEARCH_CONCURRENCY=N  RESEARCH_CLUSTERED_ONLY=1  RESEARCH_FAST_MODE=true"

# Optional: Redis for Arq prep queue (see docs/LOCAL_QUEUE.md)
redis-up:
    docker compose up -d redis

redis-down:
    docker compose down

worker:
    uv sync --extra queue
    uv run kairos worker

# GEPA when enough feedback exists (cron / Cloud Run)
optimize-nightly:
    uv run kairos optimize nightly
