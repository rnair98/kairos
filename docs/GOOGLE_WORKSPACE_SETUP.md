# Google Workspace Setup for Kairos

Google access is **not tied to the web app**. Users consent via a **loopback OAuth callback** that works from:

- **MCP tools** (`connect_google`, `start_google_connect` + `wait_google_connect`)
- **CLI** (`kairos google connect`)

Tokens are stored per user in the database (`google_connections`).

## 1. GCP OAuth client (Desktop)

1. [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services
2. Enable **Google Calendar API** and **Gmail API**
3. OAuth consent screen → External → add test users (Testing mode)
4. Credentials → **OAuth client ID → Desktop app**
5. Add authorized redirect URI:

```
http://127.0.0.1:8766/callback
```

6. Add to `.env`:

```bash
GOOGLE_CLIENT_ID=....apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
```

## 2. Connect via MCP (recommended)

Ensure Kairos MCP is running (`uv run kairos mcp`). Agent tools:

### One-shot (blocking)

```
connect_google(open_browser=false)
→ user opens authorization_url in browser
→ local callback on :8766 completes consent
→ returns { user_id, email }
```

Set `KAIROS_USER_ID` in MCP env to the returned `user_id`.

### Two-step (present URL, then wait)

```
start_google_connect() → { authorization_url, state }
# user completes consent in browser
wait_google_connect(state=...) → { user_id, email }
```

### Then sync headspace

```
sync_google_headspace(user_id=...)
run_heartbeat(delivery='return_only')
```

## 3. Connect via CLI

```bash
uv run kairos google auth-check
uv run kairos google connect --write-env
uv run kairos google verify --user-id <google-sub>
```

`--write-env` saves `KAIROS_USER_ID` for MCP and web dashboard context.

## 4. Web dashboard

The web app does **not** run OAuth. It reads the session user's fused headspace, falling back to `KAIROS_USER_ID` when no session user is present.

`GET /api/google/status` is read-only — shows whether that session/fallback user has stored tokens.

## 5. Scopes

Read-only headspace sensors:

- Calendar events / free-busy
- Gmail threads (read-only)

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Redirect URI mismatch | Register `http://127.0.0.1:8766/callback` exactly in GCP |
| Port in use | Change `GOOGLE_OAUTH_REDIRECT_URI` port or free :8766 |
| No refresh token | Revoke at myaccount.google.com/permissions, reconnect |
| MCP timeout | Increase `timeout_seconds` on `wait_google_connect` |
| Unknown OAuth state | Session expired — call `start_google_connect` again |

## Architecture

```
User → authorization_url (browser)
         ↓
http://127.0.0.1:8766/callback  (Kairos listener — MCP or CLI)
         ↓
google_connections[user_id] (libSQL)
         ↓
sync_google_headspace → context_cache[user_id] → run_heartbeat   (CLI/cron/MCP)
         OR
ADK agent → Workspace MCP (calendar + gmail) → fuse_headspace_context → run_heartbeat
```

### ADK agent path (`kairos agent-cycle`)

The ADK harness (`src/kairos/agent/agent.py`) connects to Google Workspace remote MCP:

- `https://calendarmcp.googleapis.com/mcp/v1`
- `https://gmailmcp.googleapis.com/mcp/v1`

OAuth tokens from `connect_google` / the database are injected per request via `header_provider`.
Run locally with `uv run kairos agent-cycle` or deploy with `adk deploy cloud_run`.
