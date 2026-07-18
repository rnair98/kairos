# Kairos MCP Setup

Expose Kairos policy tools to Claude Code, Cursor, and other MCP clients via **stdio FastMCP**.

## Tools exposed

| Tool | Purpose |
|------|---------|
| `connect_google` | Loopback OAuth — block until user consents in browser |
| `start_google_connect` | Return `authorization_url` + `state`; starts :8766 callback listener |
| `wait_google_connect` | Wait for user to complete consent after `start_google_connect` |
| `google_connect_status` | Poll in-flight OAuth session |
| `sync_google_headspace` | Fetch Calendar/Gmail for connected user → fuse + persist |
| `fuse_headspace_context` | Fuse raw event/thread payloads manually |
| `set_context` | Patch headspace fields directly |
| `get_current_context` | Read fused snapshot |
| `run_heartbeat` | Policy cycle → `KAIROS_OK` or `SURFACE` + digest |
| `record_feedback` | Dismiss / snooze / click → online bandit update |
| `get_cluster_summary` | Topic → nearest cluster |
| `get_relevant_bookmarks` | Semantic search over embedded bookmarks |

## Quick test

```bash
cd /path/to/kairos
uv run kairos mcp --help
# stdio server (blocks — for MCP clients):
uv run kairos mcp
```

## Claude Code / Cursor

Copy [MCP_SETUP.example.json](./MCP_SETUP.example.json) and set:

- `cwd` — absolute path to this repo
- `env` — `GEMINI_API_KEY`, `EXA_API_KEY` (optional — web grounding fails soft without it), `KAIROS_USER_ID` (after connect); storage is a local `kairos.db` file, no env var needed

```json
{
  "mcpServers": {
    "kairos": {
      "command": "uv",
      "args": ["run", "kairos", "mcp"],
      "cwd": "/Users/you/sandbox/kairos",
      "env": {
        "GEMINI_API_KEY": "...",
        "EXA_API_KEY": "...",
        "KAIROS_USER_ID": "google-sub-after-connect"
      }
    }
  }
}
```

## Google connect (MCP — not web app)

See [GOOGLE_WORKSPACE_SETUP.md](./GOOGLE_WORKSPACE_SETUP.md). Summary:

```
connect_google(open_browser=false)
# → user opens authorization_url
# → callback http://127.0.0.1:8766/callback
# → { user_id, email }
```

Or two-step: `start_google_connect` → user consents → `wait_google_connect(state=...)`.

## Runtime paths (MCP vs ADK)

| Path | Command | Sensor fetch |
|------|---------|--------------|
| **Direct** | `kairos heartbeat`, `POST /api/heartbeat`, MCP `run_heartbeat` | Demo stub, fused cache, or `sync_google_headspace` |
| **ADK agent** | `kairos agent-cycle`, `heartbeat --via-agent` | Workspace Calendar/Gmail MCP → `fuse_headspace_context` |

Both invoke the same `HeartbeatService` after headspace is fused.

## Loop Prompt (Claude Code — Kairos MCP)

```
/loop 5m
1. sync_google_headspace(user_id=...)  # connect_google first if needed
2. run_heartbeat(delivery='return_only')
If SURFACE, show delivery.rendered_markdown. On dismiss, record_feedback.
```

Browser + MCP in parallel: `just demo-serve` while the loop runs.

## Transport

| Flag | Use |
|------|-----|
| `kairos mcp` (default) | stdio — Claude Code / Cursor |
| `kairos mcp --transport sse --port 8421` | SSE — debugging |

## Environment

Loads `.env` from repo root via `pydantic-settings`. Requires Gemini (storage is a local libSQL/Turso file, no setup needed). Google OAuth uses `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` from `.env`.
