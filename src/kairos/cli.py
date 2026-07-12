"""Kairos CLI — heartbeat, agent harness, and server."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import orjson


def _dumps(obj, *, indent: bool = True, default=str) -> str:
    opts = orjson.OPT_INDENT_2 if indent else 0
    if default is not None:
        return orjson.dumps(obj, option=opts, default=default).decode()
    return orjson.dumps(obj, option=opts).decode()


def main() -> None:
    parser = argparse.ArgumentParser(description="Kairos bookmark surfacing agent")
    sub = parser.add_subparsers(dest="command", required=True)

    heartbeat = sub.add_parser("heartbeat", help="Run one policy heartbeat")
    heartbeat.add_argument(
        "--delivery",
        choices=["auto", "return_only", "none"],
        default="auto",
    )
    heartbeat.add_argument(
        "--context-override",
        default=None,
        help="Free-text headspace hint for ranking (demo override)",
    )
    heartbeat.add_argument(
        "--via-agent",
        action="store_true",
        help="ADK sensor-fusion path via Workspace MCP (slower than direct policy)",
    )

    sub.add_parser("cycle", help="Alias for heartbeat")
    feedback_parser = sub.add_parser("feedback", help="Record feedback on a surfaced notification")
    feedback_parser.add_argument("notification_id", help="Notification UUID from SURFACE heartbeat")
    feedback_parser.add_argument(
        "--action",
        required=True,
        choices=["expanded", "link_click", "snoozed", "dismissed", "acted", "ignored"],
    )
    feedback_parser.add_argument("--url", default=None, help="Link URL for link_click")
    agent_cycle = sub.add_parser(
        "agent-cycle",
        help="ADK sensor-fusion: Workspace MCP → fuse → run_heartbeat (not the default)",
    )
    sub.add_parser("chat", help="Interactive agent turn").add_argument(
        "prompt", nargs="?", default="Run one heartbeat cycle."
    )
    serve_parser = sub.add_parser("serve", help="Start web gateway (FastAPI + SSE)")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=None)

    mcp_parser = sub.add_parser("mcp", help="Run Kairos MCP server (FastMCP / stdio)")
    mcp_parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport (default: stdio for Claude Code / Cursor)",
    )
    mcp_parser.add_argument("--host", default="127.0.0.1")
    mcp_parser.add_argument("--port", type=int, default=8421)

    google_cmd = sub.add_parser("google", help="Google Workspace OAuth and headspace sensors")
    google_sub = google_cmd.add_subparsers(dest="google_command", required=True)
    google_connect = google_sub.add_parser(
        "connect",
        help="OAuth loopback callback — Calendar + Gmail (same flow as MCP connect_google)",
    )
    google_connect.add_argument(
        "--no-browser",
        action="store_true",
        help="Print authorization URL only; do not open a browser",
    )
    google_connect.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Seconds to wait for OAuth callback (default: 300)",
    )
    google_connect.add_argument(
        "--write-env",
        action="store_true",
        help="Write KAIROS_USER_ID to .env after connect",
    )
    google_connect.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to .env file (default: ./.env)",
    )
    google_sub.add_parser(
        "auth-check", help="Validate Google OAuth config and print setup checklist"
    )
    google_verify = google_sub.add_parser(
        "verify",
        help="Fetch Calendar + Gmail, fuse headspace, report signal quality",
    )
    google_verify.add_argument("--json", action="store_true", help="Output JSON report")
    google_verify.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not write fused snapshot to the database",
    )
    google_verify.add_argument(
        "--location-type",
        default=None,
        choices=["desk", "commute", "gym", "cafe", "near_anchor", "unknown"],
        help="Optional location override for fusion",
    )
    google_verify.add_argument("--lat", type=float, default=None)
    google_verify.add_argument("--lng", type=float, default=None)
    google_verify.add_argument(
        "--user-id",
        default=None,
        help="Google user id (sub) from connect_google / kairos google connect",
    )
    google_verify.add_argument(
        "--env",
        action="store_true",
        help="Use GOOGLE_REFRESH_TOKEN from .env (dev-only single user)",
    )
    google_sync = google_sub.add_parser(
        "sync",
        help="Sync Calendar + Gmail into headspace (persist to the database)",
    )
    google_sync.add_argument(
        "--user-id",
        default=None,
        help="Google user id (sub) from connect — defaults to KAIROS_USER_ID",
    )
    google_sync.add_argument(
        "--env",
        action="store_true",
        help="Use GOOGLE_REFRESH_TOKEN from .env (dev-only single user)",
    )

    x_cmd = sub.add_parser("x", help="X API utilities")
    x_sub = x_cmd.add_subparsers(dest="x_command", required=True)
    whoami_parser = x_sub.add_parser(
        "whoami", help="Print authenticated user id from GET /2/users/me"
    )
    whoami_parser.add_argument(
        "--write-env",
        action="store_true",
        help="Write numeric X_USER_ID to .env",
    )
    whoami_parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to .env file (default: ./.env)",
    )
    refresh_parser = x_sub.add_parser(
        "refresh", help="Refresh X_ACCESS_TOKEN using X_REFRESH_TOKEN"
    )
    refresh_parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to .env file to update (default: ./.env)",
    )
    refresh_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Refresh tokens but do not write .env",
    )

    auth_parser = x_sub.add_parser(
        "auth", help="OAuth 2.0 Authorization Code flow with PKCE"
    )
    auth_parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to .env file to update (default: ./.env)",
    )
    auth_parser.add_argument(
        "--redirect-uri",
        default=None,
        help="Override X_OAUTH_REDIRECT_URI (must match X app callback URL)",
    )
    auth_parser.add_argument(
        "--scope",
        default=None,
        help="Override X_OAUTH_SCOPES (space-separated)",
    )
    auth_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print authorize URL only; do not open a browser",
    )
    auth_parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Seconds to wait for OAuth callback (default: 300)",
    )
    auth_parser.add_argument(
        "--minimal-scopes",
        action="store_true",
        help="Use users.read tweet.read offline.access (skip bookmark.read for OAuth debugging)",
    )

    x_sub.add_parser("auth-check", help="Validate X OAuth config and print setup checklist")

    ingest_cmd = sub.add_parser("ingest", help="Bookmark ingest pipelines")
    ingest_sub = ingest_cmd.add_subparsers(dest="ingest_command", required=True)
    sync_parser = ingest_sub.add_parser("sync", help="Sync bookmarks from X API to the database")
    sync_parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit pagination (default: all pages)",
    )
    sync_parser.add_argument(
        "--enrich",
        action="store_true",
        help="Inline Gemini enrich during sync (default: raw sync; use bookmarks enrich/prep)",
    )
    sync_parser.add_argument(
        "--skip-enrich",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    sync_parser.add_argument(
        "--enrich-existing",
        action="store_true",
        help="Re-run enrichment even when raw_text unchanged",
    )
    sync_parser.add_argument(
        "--full",
        action="store_true",
        help="Full catalog sync (disable incremental early-stop)",
    )

    bookmarks_cmd = sub.add_parser("bookmarks", help="Read bookmarks from the database")
    bookmarks_sub = bookmarks_cmd.add_subparsers(dest="bookmarks_command", required=True)
    list_parser = bookmarks_sub.add_parser("list", help="List stored bookmarks")
    list_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max bookmarks to return (default: 20)",
    )
    list_parser.add_argument(
        "--skip",
        type=int,
        default=0,
        help="Number of bookmarks to skip (default: 0)",
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output full JSON",
    )
    get_parser = bookmarks_sub.add_parser("get", help="Get one bookmark by x_tweet_id")
    get_parser.add_argument("x_tweet_id", help="X tweet id")
    get_parser.add_argument(
        "--json",
        action="store_true",
        help="Output full JSON",
    )
    enrich_parser = bookmarks_sub.add_parser(
        "enrich", help="Backfill Gemini enrichment on stored bookmarks"
    )
    enrich_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max bookmarks to process (default: all)",
    )
    enrich_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-enrich even when consumption_mode is already set",
    )
    enrich_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be enriched without calling Gemini",
    )
    enrich_parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Parallel Gemini requests (default: ENRICH_CONCURRENCY or 10)",
    )

    research_parser = bookmarks_sub.add_parser(
        "research", help="Backfill grounded web research (summary + relevance signal)"
    )
    research_parser.add_argument("--limit", type=int, default=None, help="Max bookmarks (default: all)")
    research_parser.add_argument(
        "--force", action="store_true", help="Re-research even when already researched"
    )
    research_parser.add_argument(
        "--dry-run", action="store_true", help="List what would be researched without calling Gemini"
    )
    research_parser.add_argument(
        "--concurrency", type=int, default=None, help="Parallel Gemini requests (default: RESEARCH_CONCURRENCY or 8)"
    )
    research_parser.add_argument(
        "--clustered-only",
        action="store_true",
        help="Only research bookmarks assigned to a cluster (skips the long tail)",
    )

    prep_parser = bookmarks_sub.add_parser(
        "prep",
        help="Full data-plane prep: enrich → research → embed → cluster",
    )
    prep_parser.add_argument(
        "--sync",
        action="store_true",
        help="Run X ingest sync before prep (incremental by default)",
    )
    prep_parser.add_argument(
        "--full-sync",
        action="store_true",
        help="With --sync, paginate full X corpus (disable incremental early-stop)",
    )
    prep_parser.add_argument("--max-pages", type=int, default=None, help="Limit X sync pages")
    prep_parser.add_argument("--skip-enrich", action="store_true")
    prep_parser.add_argument("--skip-research", action="store_true")
    prep_parser.add_argument("--skip-embed", action="store_true")
    prep_parser.add_argument("--skip-cluster", action="store_true")
    prep_parser.add_argument("--research-limit", type=int, default=None)
    prep_parser.add_argument("--research-concurrency", type=int, default=None)
    prep_parser.add_argument(
        "--clustered-only",
        action="store_true",
        help="Research clustered bookmarks only",
    )

    embed_parser = bookmarks_sub.add_parser(
        "embed", help="Compute embeddings for stored bookmarks"
    )
    embed_parser.add_argument("--limit", type=int, default=None)
    embed_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-embed even when embedding exists",
    )

    cluster_parser = bookmarks_sub.add_parser(
        "cluster", help="HDBSCAN cluster bookmarks by embedding"
    )
    cluster_parser.add_argument(
        "--min-cluster-size",
        type=int,
        default=None,
        help="HDBSCAN min_cluster_size (default: HDBSCAN_MIN_CLUSTER_SIZE or 3)",
    )

    clusters_parser = bookmarks_sub.add_parser(
        "clusters", help="List persisted topic clusters"
    )
    clusters_parser.add_argument("--json", action="store_true")
    clusters_parser.add_argument("--limit", type=int, default=20)

    sim_cmd = sub.add_parser("sim", help="Persona gym — simulate user feedback for policy pre-training")
    sim_sub = sim_cmd.add_subparsers(dest="sim_command", required=True)

    sim_run = sim_sub.add_parser("run", help="Run the gym against synthetic personas")
    sim_run.add_argument(
        "--days", type=int, default=14, help="Simulated days per persona (default: 14)"
    )
    sim_run.add_argument(
        "--personas",
        default="alex,maya,jordan",
        help="Comma-separated personas to run (default: alex,maya,jordan)",
    )
    sim_run.add_argument(
        "--run-id", default=None, help="Stable run ID for tagging events (auto-generated if omitted)"
    )

    sim_reset = sim_sub.add_parser("reset", help="Delete sim feedback events and reset bandit params")
    sim_reset.add_argument(
        "--run-id", default=None, help="Limit deletion to a specific run ID (default: all sim events)"
    )

    optimize_cmd = sub.add_parser("optimize", help="GEPA digest prompt optimization")
    optimize_sub = optimize_cmd.add_subparsers(dest="optimize_command", required=True)
    optimize_run = optimize_sub.add_parser("run", help="Run one GEPA reflection pass")
    optimize_run.add_argument("--dry-run", action="store_true")
    optimize_run.add_argument("--days", type=int, default=14)
    optimize_run.add_argument("--min-samples", type=int, default=None)
    optimize_readiness = optimize_sub.add_parser(
        "readiness", help="Check if enough feedback exists for GEPA"
    )
    optimize_readiness.add_argument("--days", type=int, default=14)
    optimize_eval = optimize_sub.add_parser("eval", help="Score digest prompt on fixed fixtures")
    optimize_eval.add_argument(
        "--prompt",
        default=None,
        help="Optional prompt override (default: active optimized prompt or built-in)",
    )
    optimize_sub.add_parser(
        "nightly",
        help="Run GEPA when enough feedback exists (cron-safe)",
    ).add_argument("--dry-run", action="store_true")

    sub.add_parser(
        "worker",
        help="Start Arq worker for prep jobs (uv sync --extra queue; redis via docker compose)",
    )

    args = parser.parse_args()

    from kairos.observability.logging import setup_logging

    setup_logging()

    if args.command in ("heartbeat", "cycle"):
        delivery = getattr(args, "delivery", "auto")
        context_override = getattr(args, "context_override", None)
        if getattr(args, "via_agent", False):
            from kairos.agent.harness import run_decision_cycle_via_agent

            result = asyncio.run(run_decision_cycle_via_agent())
            print(result.model_dump_json(indent=2) if result else "No structured result")
        else:
            from kairos.agent.harness import run_decision_cycle

            result = asyncio.run(
                run_decision_cycle(delivery=delivery, context_override=context_override)
            )
            print(_dumps(result.model_dump()))
    elif args.command == "feedback":
        from kairos.core.heartbeat import heartbeat_service

        result = asyncio.run(
            heartbeat_service.record_feedback(
                args.notification_id,
                args.action,
                url=args.url,
            )
        )
        print(_dumps(result, default=str))
        if result.get("status") != "ok":
            sys.exit(1)
    elif args.command == "agent-cycle":
        print(
            "Note: agent-cycle uses ADK + Workspace MCP. "
            "For demo/dashboard use: kairos heartbeat",
            file=sys.stderr,
        )
        from kairos.agent.harness import run_decision_cycle_via_agent

        result = asyncio.run(run_decision_cycle_via_agent())
        print(result.model_dump_json(indent=2) if result else "No structured result")
    elif args.command == "chat":
        from kairos.agent.harness import run_interactive

        text = asyncio.run(run_interactive(args.prompt))
        print(text)
    elif args.command == "serve":
        from kairos.config import settings
        from kairos.web.server import run_server

        port = args.port
        if port is None:
            base = settings.web_base_url.rstrip("/")
            port = int(base.rsplit(":", 1)[-1]) if ":" in base else 8420
        print(f"Kairos dashboard → http://{args.host}:{port}")
        run_server(host=args.host, port=port)
    elif args.command == "mcp":
        from kairos.mcp.server import run_sse, run_stdio

        if args.transport == "sse":
            print(f"Kairos MCP (SSE) → http://{args.host}:{args.port}")
            run_sse(host=args.host, port=args.port)
        else:
            run_stdio()
    elif args.command == "google":
        if args.google_command == "auth-check":
            from kairos.google.auth_check import run_auth_check

            check = run_auth_check()
            print("\n".join(check.lines))
            sys.exit(0 if check.ok else 1)
        elif args.google_command == "connect":
            from kairos.google.connect import connect_google
            from kairos.google.credentials import GoogleAuthError
            from kairos.util.env_file import update_env_file

            try:
                result = asyncio.run(
                    connect_google(
                        open_browser=not args.no_browser,
                        timeout_seconds=args.timeout,
                    )
                )
            except GoogleAuthError as exc:
                print(f"✗ Google OAuth failed: {exc}", file=sys.stderr)
                sys.exit(1)

            status = result.get("status")
            if status != "connected":
                print(f"✗ Google connect failed: {result.get('message', status)}", file=sys.stderr)
                if result.get("authorization_url"):
                    print(f"  URL: {result['authorization_url']}", file=sys.stderr)
                sys.exit(1)

            user_id = result["user_id"]
            print("✓ Google connected")
            print(f"  user_id: {user_id}")
            print(f"  email:   {result.get('email')}")
            if args.write_env:
                keys = update_env_file(args.env_file, {"KAIROS_USER_ID": user_id})
                print(f"  updated: {args.env_file.resolve()} ({', '.join(keys)})")
            print("\nNext: kairos google sync --user-id", user_id)
        elif args.google_command == "sync":
            from kairos.config import settings
            from kairos.google.verify import verify_google_headspace

            user_id = args.user_id or settings.kairos_user_id
            report = asyncio.run(
                verify_google_headspace(
                    user_id=user_id,
                    persist=True,
                    use_env_fallback=args.env or not user_id,
                )
            )
            print("Google headspace sync")
            if report.user_id:
                print(f"  user_id: {report.user_id}")
            print(f"  status: {'OK' if report.ok else 'LOW SIGNAL'}")
            print(f"  calendar events: {len(report.calendar_events)}")
            print(f"  email threads:   {len(report.email_threads)}")
            for line in report.highlights:
                print(f"  ✓ {line}")
            for line in report.issues:
                print(f"  ✗ {line}")
            if report.snapshot:
                print(f"\n  context_class: {report.ctx_class}")
                print(f"  moment:        {report.moment[:200]}")
            sys.exit(0 if report.ok else 1)
        elif args.google_command == "verify":
            from kairos.google.verify import verify_google_headspace

            report = asyncio.run(
                verify_google_headspace(
                    user_id=args.user_id,
                    persist=not args.no_persist,
                    location_type=args.location_type,
                    lat=args.lat,
                    lng=args.lng,
                    use_env_fallback=args.env,
                )
            )
            if args.json:
                print(_dumps(report.to_dict()))
            else:
                print("Google headspace verification")
                if report.user_id:
                    print(f"  user_id: {report.user_id}")
                print(f"  status: {'OK' if report.ok else 'LOW SIGNAL'}")
                print(f"  calendar events: {len(report.calendar_events)}")
                print(f"  email threads:   {len(report.email_threads)}")
                for line in report.highlights:
                    print(f"  ✓ {line}")
                for line in report.issues:
                    print(f"  ✗ {line}")
                if report.snapshot:
                    print(f"\n  context_class: {report.ctx_class}")
                    print(f"  moment:        {report.moment[:200]}")
            sys.exit(0 if report.ok else 1)
        else:
            parser.print_help()
            sys.exit(1)
    elif args.command == "x":
        if args.x_command == "whoami":
            if args.write_env:
                from kairos.ingest.x.auth import persist_x_user_id

                result = asyncio.run(persist_x_user_id(args.env_file))
                print(f"✓ X_USER_ID={result.user_id}")
                if result.username:
                    print(f"  username: @{result.username}")
                if result.name:
                    print(f"  name:     {result.name}")
                print(f"  updated:  {result.env_path.resolve()}")
            else:
                from kairos.ingest.sync import fetch_x_user_id

                user_id = asyncio.run(fetch_x_user_id())
                print(user_id)
        elif args.x_command == "auth-check":
            from kairos.ingest.x.auth_check import run_auth_check

            check = run_auth_check()
            print("\n".join(check.lines))
            sys.exit(0 if check.ok else 1)
        elif args.x_command == "auth":
            from kairos.ingest.x.auth import run_pkce_auth_flow
            from kairos.ingest.x.auth_check import MINIMAL_SCOPES
            from kairos.ingest.x.client import XApiError

            scopes = args.scope
            if args.minimal_scopes:
                scopes = MINIMAL_SCOPES

            try:
                result = asyncio.run(
                    run_pkce_auth_flow(
                        args.env_file,
                        redirect_uri=args.redirect_uri,
                        scopes=scopes,
                        open_browser=not args.no_browser,
                        timeout=args.timeout,
                    )
                )
            except XApiError as exc:
                print(f"✗ OAuth failed: {exc}", file=sys.stderr)
                sys.exit(1)
            except TimeoutError as exc:
                print(f"✗ OAuth timed out: {exc}", file=sys.stderr)
                sys.exit(1)

            print("✓ OAuth authorization complete")
            print(f"  user_id:  {result.user_id}")
            if result.username:
                print(f"  username: @{result.username}")
            if result.tokens.expires_in is not None:
                print(f"  expires:  {result.tokens.expires_in}s")
            if result.tokens.scope:
                print(f"  scope:    {result.tokens.scope}")
            print(f"  updated:  {result.env_path.resolve()}")
            print(f"  keys:     {', '.join(result.keys_written)}")
        elif args.x_command == "refresh":
            from kairos.ingest.x.auth import refresh_tokens_to_env

            result = asyncio.run(
                refresh_tokens_to_env(args.env_file, dry_run=args.dry_run)
            )
            tokens = result.tokens
            print("✓ Token refresh OK")
            if tokens.expires_in is not None:
                print(f"  expires_in: {tokens.expires_in}s")
            if tokens.scope:
                print(f"  scope:      {tokens.scope}")
            if args.dry_run:
                print("  dry-run:    .env not modified")
                print(f"  access_token:  {tokens.access_token[:8]}...{tokens.access_token[-4:]}")
                if tokens.refresh_token:
                    rt = tokens.refresh_token
                    print(f"  refresh_token: {rt[:8]}...{rt[-4:]}")
            else:
                print(f"  updated:    {result.env_path.resolve()}")
                print(f"  keys:       {', '.join(result.keys_written)}")
        else:
            parser.print_help()
            sys.exit(1)
    elif args.command == "ingest":
        if args.ingest_command == "sync":
            from kairos.ingest.sync import sync_bookmarks_from_x

            result = asyncio.run(
                sync_bookmarks_from_x(
                    max_pages=args.max_pages,
                    incremental=not args.full,
                    enrich=args.enrich and not args.skip_enrich,
                    enrich_existing=args.enrich_existing,
                )
            )
            print(_dumps(result.__dict__))
            if result.errors:
                sys.exit(1)
        else:
            parser.print_help()
            sys.exit(1)
    elif args.command == "bookmarks":
        from kairos.bookmarks.cli import (
            fetch_bookmarks,
            format_bookmarks_json,
            format_bookmarks_table,
        )

        if args.bookmarks_command == "list":
            result = asyncio.run(fetch_bookmarks(limit=args.limit, skip=args.skip))
            print(format_bookmarks_json(result) if args.json else format_bookmarks_table(result))
            if result["total"] == 0:
                sys.exit(1)
        elif args.bookmarks_command == "get":
            result = asyncio.run(fetch_bookmarks(x_tweet_id=args.x_tweet_id))
            if not result["bookmarks"]:
                print(f"Bookmark not found: {args.x_tweet_id}", file=sys.stderr)
                sys.exit(1)
            if args.json:
                print(format_bookmarks_json(result))
            else:
                print(format_bookmarks_table(result))
        elif args.bookmarks_command == "enrich":
            from kairos.bookmarks.enrich import enrich_stored_bookmarks

            result = asyncio.run(
                enrich_stored_bookmarks(
                    limit=args.limit,
                    force=args.force,
                    dry_run=args.dry_run,
                    concurrency=args.concurrency,
                )
            )
            print(_dumps(result.__dict__))
            if result.errors:
                sys.exit(1)
        elif args.bookmarks_command == "research":
            from kairos.bookmarks.research import research_stored_bookmarks

            result = asyncio.run(
                research_stored_bookmarks(
                    limit=args.limit,
                    force=args.force,
                    dry_run=args.dry_run,
                    concurrency=args.concurrency,
                    clustered_only=args.clustered_only or None,
                )
            )
            print(_dumps(result.__dict__))
            if result.errors and result.researched == 0:
                sys.exit(1)
            if result.errors:
                print(
                    f"Warning: {len(result.errors)} bookmark(s) failed; "
                    f"{result.researched} succeeded. Re-run to retry failures.",
                    file=sys.stderr,
                )
        elif args.bookmarks_command == "prep":
            from kairos.bookmarks.pipeline import run_bookmark_prep

            result = asyncio.run(
                run_bookmark_prep(
                    sync=args.sync,
                    incremental_sync=not args.full_sync,
                    max_pages=args.max_pages,
                    skip_enrich=args.skip_enrich,
                    skip_research=args.skip_research,
                    skip_embed=args.skip_embed,
                    skip_cluster=args.skip_cluster,
                    research_limit=args.research_limit,
                    research_concurrency=args.research_concurrency,
                    research_clustered_only=args.clustered_only or None,
                )
            )
            print(_dumps(result.__dict__))
            errors: list[str] = []
            if result.sync and result.sync.errors:
                errors.extend(result.sync.errors)
            if result.enrich.errors:
                errors.extend(result.enrich.errors)
            if result.research and result.research.errors:
                errors.extend(result.research.errors)
            if result.embed.errors:
                errors.extend(result.embed.errors)
            if result.cluster and result.cluster.errors:
                errors.extend(result.cluster.errors)
            if errors and not (
                (result.research and result.research.researched > 0)
                or result.embed.embedded > 0
                or (result.cluster and result.cluster.clusters > 0)
            ):
                sys.exit(1)
        elif args.bookmarks_command == "embed":
            from kairos.bookmarks.index import embed_stored_bookmarks

            result = asyncio.run(
                embed_stored_bookmarks(limit=args.limit, force=args.force)
            )
            print(_dumps(result.__dict__))
            if result.errors:
                sys.exit(1)
        elif args.bookmarks_command == "cluster":
            from kairos.bookmarks.index import cluster_stored_bookmarks

            result = asyncio.run(
                cluster_stored_bookmarks(min_cluster_size=args.min_cluster_size)
            )
            print(_dumps(result.__dict__))
            if result.errors:
                sys.exit(1)
        elif args.bookmarks_command == "clusters":
            from kairos.bookmarks.index import fetch_cluster_catalog

            clusters = asyncio.run(fetch_cluster_catalog())
            clusters = clusters[: args.limit]
            if args.json:
                printable = []
                for cluster in clusters:
                    item = dict(cluster)
                    if item.get("_id"):
                        item["id"] = str(item.pop("_id"))
                    if item.get("last_updated") and hasattr(item["last_updated"], "isoformat"):
                        item["last_updated"] = item["last_updated"].isoformat()
                    printable.append(item)
                print(_dumps(printable))
            else:
                if not clusters:
                    print("No clusters — run: kairos bookmarks embed && kairos bookmarks cluster")
                    sys.exit(1)
                print(f"Clusters ({len(clusters)} shown)\n")
                for cluster in clusters:
                    name = cluster.get("name") or "(unnamed)"
                    count = cluster.get("member_count", 0)
                    summary = (cluster.get("summary") or "").replace("\n", " ")[:100]
                    cid = cluster.get("cluster_id") or "?"
                    print(f"{cid[:8]}…  {name} ({count})")
                    if summary:
                        print(f"  {summary}…")
                    print()
        else:
            parser.print_help()
            sys.exit(1)
    elif args.command == "sim":
        if args.sim_command == "run":
            from kairos.sim.gym import run_gym
            from kairos.sim.persona import ALL_PERSONAS

            persona_names = [n.strip() for n in args.personas.split(",") if n.strip()]
            unknown = [n for n in persona_names if n not in ALL_PERSONAS]
            if unknown:
                print(f"Unknown personas: {unknown}. Available: {list(ALL_PERSONAS)}", file=sys.stderr)
                sys.exit(1)

            personas = [ALL_PERSONAS[n] for n in persona_names]
            print(f"Running gym: personas={persona_names} days={args.days}")
            result = asyncio.run(run_gym(personas=personas, days=args.days, run_id=args.run_id))
            print(f"\n✓ Gym complete  run_id={result.run_id}")
            print(f"  ticks={result.total_ticks}  surfaces={result.total_surfaces}  engagements={result.total_engagements}")
            print(f"  overall engagement rate: {result.overall_rate:.1%}")
            for persona_name, rates in result.engagement_by_day.items():
                if rates:
                    trend = " ".join(f"{r:.0%}" for r in rates[::max(1, len(rates)//7)])
                    print(f"  {persona_name}: {trend}")
            if result.errors:
                print(f"\n  ⚠ {len(result.errors)} errors (first: {result.errors[0]})", file=sys.stderr)
                sys.exit(1)

        elif args.sim_command == "reset":
            from kairos.sim.gym import reset_gym

            run_id = getattr(args, "run_id", None)
            result = asyncio.run(reset_gym(run_id=run_id))
            print(f"✓ Sim reset")
            print(f"  deleted feedback_events: {result['deleted_feedback_events']}")
            print(f"  reset bandit_params:      {result['reset_bandit_params']}")
        else:
            parser.print_help()
            sys.exit(1)
    elif args.command == "optimize":
        if args.optimize_command == "run":
            from kairos.core.optimize import run_gepa

            result = asyncio.run(
                run_gepa(
                    dry_run=args.dry_run,
                    days=args.days,
                    min_samples=args.min_samples,
                )
            )
            print(_dumps(result.model_dump(mode="json")))
        elif args.optimize_command == "readiness":
            from kairos.config import settings
            from kairos.core.eval_harness import feedback_readiness

            result = asyncio.run(
                feedback_readiness(days=args.days, min_samples=settings.gepa_min_samples)
            )
            print(_dumps(result.model_dump(mode="json")))
        elif args.optimize_command == "eval":
            from kairos.core.eval_harness import run_fixture_eval

            result = asyncio.run(run_fixture_eval(prompt_override=args.prompt))
            print(_dumps(result.model_dump(mode="json")))
        elif args.optimize_command == "nightly":
            from kairos.core.gepa_cron import run_gepa_nightly

            dry_run = getattr(args, "dry_run", False)
            result = asyncio.run(run_gepa_nightly(dry_run=dry_run))
            print(_dumps(result.model_dump(mode="json")))
        else:
            parser.print_help()
            sys.exit(1)
    elif args.command == "worker":
        from kairos.jobs.worker import main as worker_main

        worker_main()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
