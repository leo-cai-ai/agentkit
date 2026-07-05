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
    # Max output (completion) tokens per call. None = use the endpoint default,
    # which is often small and gets consumed by a reasoning model's <think>
    # block before the JSON answer is emitted (truncated output -> "LLM did not
    # return a valid JSON object"). Set this high (e.g. 4096) for reasoning
    # models (DeepSeek-R1, Qwen-thinking, etc.) so the answer can finish.
    llm_max_tokens: int | None = Field(default=None, gt=0)
    llm_context_window_tokens: int = Field(default=128_000, gt=0)
    runtime_environment: Literal["development", "test", "production"] = "production"
    context_debug_rendered_enabled: bool = False
    llm_requests_per_second: float = Field(default=0.9, gt=0)
    llm_rate_limiter_enabled: bool = True
    # Rate-limiter backend. 'process' is LangChain's in-memory token bucket
    # (per-process: effective rate = workers x requests_per_second). 'sqlite'
    # shares one bucket across all workers on a host via a SQLite file, so the
    # configured rate holds regardless of worker count — use it behind a
    # spike-arrest endpoint when running multiple gunicorn workers.
    llm_rate_limiter_backend: Literal["process", "sqlite"] = "process"
    llm_rate_limiter_sqlite_path: str | None = None

    # Durable runtime storage for audit/run history and conversation messages.
    # SQLite remains the zero-dependency local default; Docker/enterprise
    # deployments should set this to "postgres" so all durable runtime state
    # lives in the configured PostgreSQL database.
    storage_backend: Literal["sqlite", "postgres"] = "sqlite"
    artifact_max_payload_bytes: int = Field(default=1_048_576, gt=0)

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
    tool_max_workers: int = Field(default=32, ge=1)
    tool_max_retries: int = Field(default=0, ge=0)
    tool_retry_base_delay: float = Field(default=0.2, ge=0.0)

    # 统一自主执行的全局硬上限；Agent 和 Skill 只能进一步收紧。
    autonomy_max_model_calls: int = Field(default=64, gt=0)
    autonomy_max_tool_calls: int = Field(default=128, gt=0)
    autonomy_max_iterations: int = Field(default=32, gt=0)
    autonomy_max_plan_steps: int = Field(default=32, gt=0)
    autonomy_max_replans: int = Field(default=4, ge=0)
    autonomy_max_tokens: int = Field(default=200_000, gt=0)
    autonomy_timeout_seconds: float = Field(default=3600.0, gt=0)

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

    # Human-approval checkpointing. "memory" pauses the graph at the approval
    # gate and resumes in-place (no full re-run); "sqlite"/"postgres" persist
    # checkpoints so a paused task survives restarts and is resumable across
    # processes/workers; "none" uses waiting output plus a protected full
    # resubmit to approve.
    approval_checkpointer: Literal["memory", "sqlite", "postgres", "none"] = "memory"

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
    # Turn off a reasoning model's <think> output. Most OpenAI-compatible servers
    # (vLLM / SGLang serving Qwen3, etc.) accept it via
    # extra_body={"chat_template_kwargs": {"enable_thinking": false}}, which this
    # flag sends. Governance/JSON calls don't benefit from chain-of-thought, so
    # disabling it is faster, cheaper, and avoids truncated-JSON errors. Harmless
    # on endpoints that ignore the flag (e.g. DeepSeek-R1, which can't disable it).
    openai_disable_thinking: bool = False
    # Raw JSON merged into the OpenAI request body (extra_body) for endpoint-
    # specific params, e.g. '{"enable_thinking": false}' or
    # '{"chat_template_kwargs": {"enable_thinking": false}}'. Merged on top of the
    # openai_disable_thinking convenience above.
    openai_extra_body: str = ""

    # Web management console security.
    web_auth_token: SecretStr | None = None
    web_secret_key: SecretStr | None = None
    web_cookie_secure: bool = True
    web_auth_disabled: bool = False

    # Identity & console RBAC. Shared-token login maps to this subject/roles.
    web_token_subject: str = "console-admin"
    web_token_roles: str = "admin"
    # Trusted tenant/business roles for shared-token login. Empty means the web
    # layer falls back to tenant ui.default_roles for local/demo operation.
    web_token_business_roles: str = ""
    # SSO via a reverse proxy that terminates OIDC/SAML and forwards identity
    # headers (oauth2-proxy / API gateway). Only trust these headers when the
    # proxy is the sole ingress; do not expose the app directly to clients.
    auth_proxy_enabled: bool = False
    auth_proxy_user_header: str = "X-Forwarded-User"
    auth_proxy_email_header: str = "X-Forwarded-Email"
    auth_proxy_roles_header: str = "X-Forwarded-Roles"
    auth_proxy_default_roles: str = "viewer"
    auth_proxy_business_roles_header: str = "X-Forwarded-Business-Roles"
    auth_proxy_default_business_roles: str = ""
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
    # backends (e.g. chroma / sqlite-vec / milvus) can be added behind
    # the VectorStore protocol without changing the retriever or its callers.
    vector_store_backend: Literal["sqlite", "postgres"] = "sqlite"

    # Enterprise knowledge-base RAG. Disabled by default; when enabled, chat
    # agents retrieve tenant-scoped knowledge chunks and inject cited snippets
    # into the prompt. Chroma is the default persistent KB store, imported lazily
    # so non-RAG deployments do not need the optional dependency.
    rag_enabled: bool = False
    rag_store_backend: Literal["chroma", "memory"] = "chroma"
    rag_chroma_path: str = "data/chroma"
    rag_chroma_collection: str = "agentkit_knowledge"
    rag_chunk_max_chars: int = Field(default=1200, gt=0)
    rag_chunk_overlap_chars: int = Field(default=120, ge=0)
    rag_table_chunk_max_chars: int = Field(default=900, gt=0)
    rag_ocr_chunk_max_chars: int = Field(default=900, gt=0)
    rag_keyword_weight: float = Field(default=0.4, ge=0.0)
    rag_vector_weight: float = Field(default=0.6, ge=0.0)
    rag_min_vector_score: float = Field(default=0.0, ge=0.0, le=1.0)
    rag_query_rewrite: Literal["none", "llm"] = "none"
    rag_query_rewrite_max: int = Field(default=3, ge=1)
    rag_reranker: Literal["none", "keyword", "llm"] = "none"
    rag_rerank_candidates: int = Field(default=12, ge=1)
    rag_top_k: int = Field(default=5, ge=0)
    rag_context_cap_tokens: int = Field(default=1000, ge=0)
    rag_ocr_enabled: bool = False

    # XHS 与 RAG 共用同一 OCR 基础设施；none 是零网络调用的全局硬关闭。
    ocr_provider: str = "none"
    ocr_url: str = "http://localhost:11434/api/generate"
    ocr_model: str = "glm-ocr:latest"
    ocr_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    ocr_max_image_bytes: int = Field(
        default=10 * 1024 * 1024,
        gt=0,
        le=50 * 1024 * 1024,
    )

    # Browser-backed public-web research. Browser lifecycle is shared across
    # site adapters; each site gets an isolated persistent profile directory so
    # login cookies are not mixed between connectors. Playwright is optional and
    # imported only when a browser provider is selected.
    xhs_research_provider: Literal["mock", "playwright"] = "mock"
    xhs_publishing_provider: Literal["mock", "playwright"] = "mock"
    xhs_base_url: str = "https://www.xiaohongshu.com"
    xhs_publish_url: str = "https://creator.xiaohongshu.com/publish/publish?source=official"
    xhs_publish_asset_root: str = "data/xhs-publish-assets"
    xhs_publish_ledger_path: str = "data/xhs-publish-ledger.sqlite"
    xhs_publish_media_strategy: Literal["upload", "xhs_text_image"] = "upload"
    xhs_text_image_style: str = "涂鸦"
    xhs_text_image_generation_timeout_seconds: float = Field(default=120.0, gt=0.0)
    xhs_enrich_details: bool = True
    xhs_detail_limit: int = Field(default=5, ge=0, le=20)
    xhs_detail_timeout_seconds: float = Field(default=6.0, gt=0.0)
    xhs_detail_pause_seconds: float = Field(default=0.5, ge=0.0)
    # 媒体理解能力通过开放注册表校验，便于后续接入 OCR、多模态或 MCP Provider。
    media_understanding_provider: str = "none"
    media_understanding_max_images: int = Field(default=3, ge=0, le=20)
    media_understanding_min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    web_search_browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    web_search_headless: bool = True
    web_search_timeout_seconds: float = Field(default=30.0, gt=0.0)
    web_search_max_scrolls: int = Field(default=6, ge=0, le=50)
    web_search_scroll_pause_seconds: float = Field(default=0.75, ge=0.0)
    web_search_profile_root: str | None = "data/browser-profiles"
    web_search_storage_state_root: str | None = None
    web_search_browser_channel: str | None = None
    web_search_executable_path: str | None = None
    browser_publish_observation_seconds: float = Field(default=90.0, ge=0.0, le=300.0)

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
