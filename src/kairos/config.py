"""Application configuration."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash"
    gemini_flash_lite_model: str = "gemini-3.1-flash-lite"
    digest_use_google_search: bool = True
    digest_skip_search_evergreen: bool = True
    intelligence_headspace_enabled: bool = True
    intelligence_digest_multistep: bool = True
    intelligence_digest_runtime_fast: bool = False
    intelligence_moment_fit_check: bool = True
    intelligence_max_sensor_chars: int = 6000
    intelligence_narrative_ttl_seconds: int = 900
    intelligence_skip_demo_narrative: bool = True
    cluster_naming_use_llm: bool = True
    cluster_label_concurrency: int = 5
    enrich_concurrency: int = 10
    enrich_max_input_chars: int = 2000

    # Fetch linked article HTML before Gemini research (not X API re-fetch)
    link_fetch_enabled: bool = True
    link_fetch_timeout_seconds: float = 15.0
    link_fetch_max_bytes: int = 2_000_000
    link_fetch_max_body_chars: int = 6000
    link_fetch_user_agent: str = "Kairos/0.1 (bookmark research bot)"
    link_fetch_concurrency: int = 20

    # Bookmark research (kairos bookmarks research)
    research_concurrency: int = 8
    research_fast_mode: bool = False  # flash-lite, no Google Search when link body fetched
    research_min_link_chars_for_fast: int = 200
    research_clustered_only: bool = False  # skip unclustered bookmarks

    # Log full Gemini request/response to stderr (and optional file)
    gemini_log_io: bool = False
    gemini_log_io_max_chars: int = 4000
    gemini_log_io_path: str | None = None  # e.g. logs/gemini-io.log

    embedding_backend: str = "gemini"  # gemini (Cloud Run) | local (offline dev)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    gemini_embedding_model: str = "gemini-embedding-001"
    gemini_embedding_dimensions: int = 768
    embedding_batch_size: int = 32
    embedding_max_input_chars: int = 2000
    hdbscan_min_cluster_size: int = 3
    hdbscan_min_samples: int = 2
    cluster_id_reuse_threshold: float = 0.88

    event_persist_enabled: bool = True
    event_persist_ttl_days: int = 7

    # Job queue — local (FastAPI background) | arq (Redis)
    job_backend: str = "local"
    redis_url: str = "redis://127.0.0.1:6379"

    # GEPA prompt optimization
    gepa_enabled: bool = True
    gepa_min_samples: int = 5

    # Bandit cold-start — blend cohort mean α/β from other users on same cluster×context
    cohort_prior_enabled: bool = True
    cohort_prior_min_users: int = 2

    # Default heartbeat path is direct policy core; ADK agent for MCP sensor fusion
    heartbeat_default_via_agent: bool = False

    # Storage — libSQL (Turso) local file; optional embedded-replica sync to a
    # hosted Turso primary when both sync settings are present.
    kairos_db_path: str = "kairos.db"
    turso_database_url: str | None = None
    turso_auth_token: str | None = None
    vector_search_enabled: bool = True

    # X API v2 — https://docs.x.com/x-api/users/get-bookmarks
    x_api_base_url: str = "https://api.x.com"
    x_access_token: str | None = None
    x_refresh_token: str | None = None
    x_client_id: str | None = None
    x_client_secret: str | None = None
    x_user_id: str | None = None
    x_oauth_redirect_uri: str = "http://127.0.0.1:8765/callback"
    x_oauth_scopes: str = "bookmark.read tweet.read users.read offline.access"

    # Google Workspace — OAuth for CLI verify + Cursor remote MCP auth block
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_access_token: str | None = None
    google_refresh_token: str | None = None
    google_oauth_redirect_uri: str = "http://127.0.0.1:8766/callback"
    google_oauth_timeout_seconds: float = 300.0
    kairos_user_id: str | None = None  # MCP/CLI: active user after connect_google
    google_calendar_credentials_path: str | None = None  # deprecated — use google auth + MCP

    # Demo mode — manual surface trigger, no auto-heartbeat on dashboard load
    demo_mode: bool = False
    auto_heartbeat: bool = True

    # Agent loop interval (seconds) for demo /loop parity
    decision_interval_seconds: int = 300

    daily_surface_budget: int = 3
    min_gap_between_surfaces_minutes: int = 45
    surface_score_threshold: float = 0.12
    min_calendar_gap_minutes: int = 30
    snooze_ttl_minutes: int = 120

    # Delivery adapters (comma-separated: web, os)
    delivery_targets: str = "web"
    web_base_url: str = "http://localhost:8420"
    os_delivery_enabled: bool = False
    mcp_suppress_ok_in_chat: bool = True

    # Logging (loguru)
    log_level: str = "INFO"
    log_json: bool = False
    log_access: bool = True
    log_pipeline_events: bool = True
    log_backtrace: bool = False
    log_diagnose: bool = False

    # Headspace sensor defaults (override via set_context / fuse_headspace)
    kairos_location_type: str = "unknown"
    kairos_location_anchors: str = ""  # "lat,lng,label,radius_km;..."

    def delivery_target_list(self) -> list[str]:
        return [t.strip() for t in self.delivery_targets.split(",") if t.strip()]

    def location_anchors(self) -> list[dict[str, float | str]]:
        """Parse KAIROS_LOCATION_ANCHORS env — geofence → location_type."""
        anchors: list[dict[str, float | str]] = []
        for chunk in self.kairos_location_anchors.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = [p.strip() for p in chunk.split(",")]
            if len(parts) < 3:
                continue
            try:
                anchors.append(
                    {
                        "lat": float(parts[0]),
                        "lng": float(parts[1]),
                        "label": parts[2],
                        "radius_km": float(parts[3]) if len(parts) > 3 else 0.25,
                    }
                )
            except ValueError:
                continue
        return anchors


settings = Settings()
