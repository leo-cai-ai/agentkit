"""Typed runtime configuration (env / .env driven, import-safe)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTKIT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: Literal["customer_band", "openai", "fake"] = "customer_band"
    llm_max_retries: int = Field(default=2, ge=0)
    llm_timeout_seconds: float = 30.0
    llm_retry_base_delay: float = 0.5
    llm_requests_per_second: float = Field(default=0.9, gt=0)
    llm_rate_limiter_enabled: bool = True
    # Rate-limiter backend. 'process' is LangChain's in-memory token bucket
    # (per-process: effective rate = workers x requests_per_second). 'sqlite'
    # shares one bucket across all workers on a host via a SQLite file, so the
    # configured rate holds regardless of worker count — use it behind a
    # spike-arrest endpoint when running multiple gunicorn workers.
    llm_rate_limiter_backend: Literal["process", "sqlite"] = "process"
    llm_rate_limiter_sqlite_path: str | None = None

    # LLM resilience: ordered fallback providers tried when the primary fails,
    # each guarded by a per-provider circuit breaker. Comma-separated provider
    # keys, e.g. "openai" or "openai,fake". Empty -> single-provider (no failover).
    llm_fallback_providers: str = ""
    # Consecutive failures before a provider's breaker opens (skips it).
    llm_circuit_failure_threshold: int = Field(default=3, ge=1)
    # Seconds a breaker stays open before a half-open trial is allowed.
    llm_circuit_reset_seconds: float = Field(default=30.0, gt=0)

    # Cost & token accounting. Prices are USD per 1K tokens for the configured
    # model; 0 leaves cost at 0 while token counts are still recorded. When
    # llm_run_budget_usd > 0, a run's LLM calls fail once accumulated cost exceeds
    # the cap (fail-closed budget guard).
    cost_tracking_enabled: bool = True
    llm_price_input_per_1k: float = Field(default=0.0, ge=0.0)
    llm_price_output_per_1k: float = Field(default=0.0, ge=0.0)
    llm_run_budget_usd: float = Field(default=0.0, ge=0.0)

    # Tool/connector execution hardening (consumed by the ToolExecutor). Retries
    # only apply to calls that are safe to repeat (tool marked idempotent or an
    # _idempotency_key supplied); default 0 keeps non-idempotent side effects safe.
    tool_timeout_seconds: float = Field(default=30.0, ge=0.0)
    tool_max_retries: int = Field(default=0, ge=0)
    tool_retry_base_delay: float = Field(default=0.2, ge=0.0)

    # Outbound egress policy for SSRF-safe tool HTTP (agentkit.core.net). By
    # default only https to public IPs is allowed; set egress_allowed_domains to a
    # comma-separated allow-list to restrict further. egress_allow_http permits
    # plain http (discouraged outside trusted networks).
    egress_allow_http: bool = False
    egress_allowed_domains: str = ""
    egress_max_response_bytes: int = Field(default=5_000_000, ge=0)
    egress_timeout_seconds: float = Field(default=10.0, ge=0.0)

    # OpenTelemetry tracing (optional; install the 'otel' extra). When disabled or
    # the SDK is absent, tracing is a no-op. The OTLP exporter endpoint is read
    # from the standard OTEL_EXPORTER_OTLP_ENDPOINT env var; tracing_console_export
    # prints spans to stdout for local debugging.
    tracing_enabled: bool = False
    tracing_service_name: str = "agentkit"
    tracing_console_export: bool = False

    # Deterministic fast-path: when the rule-based router resolves a skill with
    # *high* confidence, skip the advisory governance LLM calls
    # (intent/route/plan/plan_review/approval-assessment) and use the
    # deterministic results instead. Requests the router can't resolve
    # confidently still run the full LLM pipeline. Off by default so governance
    # visibility is unchanged unless explicitly opted in.
    deterministic_fastpath: bool = False

    # Combined intent+route: when the request must go through the LLM (fast-path
    # did not engage), resolve the IntentFrame and the routed skill in a single
    # LLM round trip instead of two. The route node then only validates the
    # suggestion deterministically. Off by default. Complements the fast-path:
    # fast-path handles rule-resolvable requests (0 LLM), this halves the round
    # trips for the rest (intent+route: 2 -> 1).
    combined_intent_route: bool = False

    # Human-approval checkpointing. "memory" pauses the graph at the approval
    # gate and resumes in-place (no full re-run); "sqlite" does the same but
    # persists checkpoints on disk so a paused task survives restarts and is
    # resumable across processes/workers; "none" keeps the legacy path
    # (waiting output + full resubmit to approve).
    approval_checkpointer: Literal["memory", "sqlite", "none"] = "memory"

    # AI provider credentials — vendor-neutral naming (consumed by the
    # customer_band provider). Canonical env vars are AI_CLIENT_ID /
    # AI_CLIENT_SECRET / AI_APP_KEY; the CUSTOMER_BAND_* names are also accepted.
    ai_client_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AI_CLIENT_ID", "CUSTOMER_BAND_CLIENT_ID"),
    )
    ai_client_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("AI_CLIENT_SECRET", "CUSTOMER_BAND_CLIENT_SECRET"),
    )
    ai_app_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("AI_APP_KEY", "CUSTOMER_BAND_APP_KEY"),
    )

    # OpenAI-compatible (read as AGENTKIT_OPENAI_*).
    openai_base_url: str | None = None
    openai_api_key: SecretStr | None = None
    openai_model: str | None = None
    openai_api_version: str | None = None

    # Web management console security.
    web_auth_token: SecretStr | None = None
    web_secret_key: SecretStr | None = None
    web_cookie_secure: bool = True
    web_auth_disabled: bool = False

    # Identity & console RBAC. Shared-token login maps to this subject/roles.
    web_token_subject: str = "console-admin"
    web_token_roles: str = "admin"
    # SSO via a reverse proxy that terminates OIDC/SAML and forwards identity
    # headers (oauth2-proxy / API gateway). Only trust these headers when the
    # proxy is the sole ingress; do not expose the app directly to clients.
    auth_proxy_enabled: bool = False
    auth_proxy_user_header: str = "X-Forwarded-User"
    auth_proxy_email_header: str = "X-Forwarded-Email"
    auth_proxy_roles_header: str = "X-Forwarded-Roles"
    auth_proxy_default_roles: str = "viewer"
    # Optional role->permission override as a JSON object, e.g.
    # '{"operator": ["task:run","task:approve"]}'. Merged over the built-in roles.
    rbac_role_permissions: str = ""

    # Content safety guardrails (PII detection + prompt-injection detection).
    safety_enabled: bool = True
    # When true, high-severity prompt-injection input is refused before any LLM
    # call. Default false (flag + audit only) to avoid false-positive blocking.
    safety_block_on_injection: bool = False
    safety_detect_pii: bool = True

    # Conversational memory (Phase 4). Only applies to memory-enabled agents.
    memory_window_turns: int = Field(default=6, ge=1)
    memory_max_context_tokens: int = Field(default=4000, gt=0)
    memory_response_reserve_tokens: int = Field(default=512, ge=0)
    memory_summary_cap_tokens: int = Field(default=600, ge=0)
    memory_retrieval_k: int = Field(default=4, ge=0)
    memory_extract_every_n_turns: int = Field(default=3, ge=1)
    memory_dedup_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    memory_min_retrieval_score: float = Field(default=0.1, ge=0.0, le=1.0)

    # Embeddings for semantic long-term memory (Phase 4b). Default 'fake' is
    # offline-safe; 'openai' uses an OpenAI-compatible /embeddings endpoint.
    embedding_provider: Literal["fake", "openai"] = "fake"
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str | None = None

    # Backend for long-term-memory vector storage + nearest-neighbour search.
    # 'sqlite' (default) keeps the per-tenant SQLite `memories` table with a
    # linear cosine scan (sufficient at the per-user scope this targets). Other
    # backends (e.g. chroma / sqlite-vec / pgvector / milvus) can be added behind
    # the VectorStore protocol without changing the retriever or its callers.
    vector_store_backend: Literal["sqlite", "chroma", "postgres"] = "sqlite"

    # PostgreSQL connection (used when a backend is set to 'postgres', e.g.
    # vector_store_backend=postgres with the pgvector extension). Either set a
    # full DSN/URL via AGENTKIT_PG_DSN, or the individual parts below. Requires
    # the optional dependency: pip install 'agentkit[pg]'.
    pg_dsn: str | None = None
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_database: str = "agentkit"
    pg_user: str = "agentkit"
    pg_password: SecretStr | None = None
    pg_sslmode: str = "prefer"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
