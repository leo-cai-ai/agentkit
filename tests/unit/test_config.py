import agentkit.config as config_mod


def _fresh_settings(monkeypatch, **env):
    for var in [
        "AGENTKIT_LLM_PROVIDER",
        "AGENTKIT_LLM_MAX_RETRIES",
        "AGENTKIT_LLM_REQUESTS_PER_SECOND",
        "AGENTKIT_LLM_RATE_LIMITER_ENABLED",
        "AI_CLIENT_ID",
        "AI_CLIENT_SECRET",
        "AI_APP_KEY",
        "CUSTOMER_BAND_CLIENT_ID",
        "CUSTOMER_BAND_CLIENT_SECRET",
        "CUSTOMER_BAND_APP_KEY",
        "AGENTKIT_OPENAI_BASE_URL",
        "AGENTKIT_OPENAI_API_KEY",
        "AGENTKIT_OPENAI_MODEL",
        "AGENTKIT_WEB_AUTH_TOKEN",
        "AGENTKIT_WEB_SECRET_KEY",
        "AGENTKIT_WEB_COOKIE_SECURE",
        "AGENTKIT_WEB_AUTH_DISABLED",
        "AGENTKIT_WEB_TOKEN_BUSINESS_ROLES",
        "AGENTKIT_AUTH_PROXY_BUSINESS_ROLES_HEADER",
        "AGENTKIT_AUTH_PROXY_DEFAULT_BUSINESS_ROLES",
        "AGENTKIT_TOOL_MAX_WORKERS",
        "AGENTKIT_VECTOR_STORE_BACKEND",
        "AGENTKIT_RAG_ENABLED",
        "AGENTKIT_RAG_STORE_BACKEND",
        "AGENTKIT_RAG_QUERY_REWRITE",
        "AGENTKIT_RAG_RERANKER",
        "AGENTKIT_XHS_RESEARCH_PROVIDER",
        "AGENTKIT_XHS_BASE_URL",
        "AGENTKIT_XHS_ENRICH_DETAILS",
        "AGENTKIT_XHS_DETAIL_LIMIT",
        "AGENTKIT_XHS_DETAIL_TIMEOUT_SECONDS",
        "AGENTKIT_XHS_DETAIL_PAUSE_SECONDS",
        "AGENTKIT_WEB_SEARCH_BROWSER",
        "AGENTKIT_WEB_SEARCH_HEADLESS",
        "AGENTKIT_WEB_SEARCH_TIMEOUT_SECONDS",
        "AGENTKIT_WEB_SEARCH_MAX_SCROLLS",
        "AGENTKIT_WEB_SEARCH_SCROLL_PAUSE_SECONDS",
        "AGENTKIT_WEB_SEARCH_PROFILE_ROOT",
        "AGENTKIT_WEB_SEARCH_STORAGE_STATE_ROOT",
        "AGENTKIT_WEB_SEARCH_BROWSER_CHANNEL",
        "AGENTKIT_WEB_SEARCH_EXECUTABLE_PATH",
        "AGENTKIT_MEMORY_WINDOW_TURNS",
        "AGENTKIT_MEMORY_MAX_CONTEXT_TOKENS",
        "AGENTKIT_ARTIFACT_MAX_PAYLOAD_BYTES",
    ]:
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return config_mod.Settings(_env_file=None)


def test_defaults(monkeypatch):
    s = _fresh_settings(monkeypatch)
    assert s.llm_provider == "customer_band"
    assert s.llm_max_retries == 2
    assert s.ai_client_id is None
    assert s.llm_requests_per_second == 0.9
    assert s.llm_rate_limiter_enabled is True
    assert s.deterministic_fastpath is False
    assert s.tool_max_workers == 32
    assert s.web_token_business_roles == ""
    assert s.auth_proxy_business_roles_header == "X-Forwarded-Business-Roles"
    assert s.xhs_research_provider == "mock"
    assert s.xhs_publish_media_strategy == "upload"
    assert s.xhs_text_image_style == "涂鸦"
    assert s.xhs_text_image_generation_timeout_seconds == 120.0
    assert s.xhs_detail_timeout_seconds == 6.0
    assert s.web_search_browser == "chromium"
    assert s.web_search_headless is True
    assert s.web_search_profile_root == "data/browser-profiles"
    assert s.web_search_storage_state_root is None
    assert s.artifact_max_payload_bytes == 1_048_576


def test_rate_limit_env_overrides(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        AGENTKIT_LLM_REQUESTS_PER_SECOND="2.5",
        AGENTKIT_LLM_RATE_LIMITER_ENABLED="false",
    )
    assert s.llm_requests_per_second == 2.5
    assert s.llm_rate_limiter_enabled is False


def test_ai_provider_env_aliases(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        AI_CLIENT_ID="cid",
        AI_CLIENT_SECRET="sec",
        AI_APP_KEY="ak",
    )
    assert s.ai_client_id == "cid"
    assert s.ai_client_secret.get_secret_value() == "sec"
    assert s.ai_app_key.get_secret_value() == "ak"


def test_customer_band_env_aliases_accepted(monkeypatch):
    # The CUSTOMER_BAND_* names are accepted as aliases for the AI_* credentials.
    s = _fresh_settings(
        monkeypatch,
        CUSTOMER_BAND_CLIENT_ID="cid",
        CUSTOMER_BAND_CLIENT_SECRET="sec",
        CUSTOMER_BAND_APP_KEY="ak",
    )
    assert s.ai_client_id == "cid"
    assert s.ai_client_secret.get_secret_value() == "sec"
    assert s.ai_app_key.get_secret_value() == "ak"


def test_secrets_are_secretstr_and_redacted(monkeypatch):
    import pydantic

    s = _fresh_settings(
        monkeypatch,
        AI_CLIENT_SECRET="supersecret",
        AI_APP_KEY="appkey123",
        AGENTKIT_OPENAI_API_KEY="sk-secret",
        AGENTKIT_WEB_AUTH_TOKEN="tok-secret",
    )
    assert isinstance(s.ai_client_secret, pydantic.SecretStr)
    assert isinstance(s.openai_api_key, pydantic.SecretStr)
    assert isinstance(s.web_auth_token, pydantic.SecretStr)
    # repr / str must not leak the plaintext secret.
    blob = repr(s) + str(s)
    for secret in ["supersecret", "appkey123", "sk-secret", "tok-secret"]:
        assert secret not in blob
    assert s.web_auth_token.get_secret_value() == "tok-secret"


def test_web_security_defaults(monkeypatch):
    s = _fresh_settings(monkeypatch)
    assert s.web_auth_token is None
    assert s.web_cookie_secure is True
    assert s.web_auth_disabled is False


def test_provider_selection_and_openai_fields(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        AGENTKIT_LLM_PROVIDER="openai",
        AGENTKIT_OPENAI_BASE_URL="http://localhost:8000/v1",
        AGENTKIT_OPENAI_API_KEY="k",
        AGENTKIT_OPENAI_MODEL="m",
    )
    assert s.llm_provider == "openai"
    assert s.openai_base_url == "http://localhost:8000/v1"
    assert s.openai_model == "m"


def test_invalid_provider_rejected(monkeypatch):
    import pydantic

    raised = False
    try:
        _fresh_settings(monkeypatch, AGENTKIT_LLM_PROVIDER="bogus")
    except pydantic.ValidationError:
        raised = True
    assert raised


def test_invalid_vector_store_backend_rejected(monkeypatch):
    import pydantic

    raised = False
    try:
        _fresh_settings(monkeypatch, AGENTKIT_VECTOR_STORE_BACKEND="chroma")
    except pydantic.ValidationError:
        raised = True
    assert raised


def test_storage_backend_env_overrides(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        AGENTKIT_STORAGE_BACKEND="postgres",
        AGENTKIT_APPROVAL_CHECKPOINTER="postgres",
    )
    assert s.storage_backend == "postgres"
    assert s.approval_checkpointer == "postgres"


def test_invalid_storage_backend_rejected(monkeypatch):
    import pydantic

    raised = False
    try:
        _fresh_settings(monkeypatch, AGENTKIT_STORAGE_BACKEND="s3")
    except pydantic.ValidationError:
        raised = True
    assert raised


def test_memory_defaults(monkeypatch):
    s = _fresh_settings(monkeypatch)
    assert s.memory_window_turns == 6
    assert s.memory_max_context_tokens == 4000
    assert s.memory_response_reserve_tokens == 512
    assert s.memory_summary_cap_tokens == 600
    assert s.memory_retrieval_k == 4
    assert s.memory_extract_every_n_turns == 3


def test_memory_env_overrides(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        AGENTKIT_MEMORY_WINDOW_TURNS="3",
        AGENTKIT_MEMORY_MAX_CONTEXT_TOKENS="1200",
    )
    assert s.memory_window_turns == 3
    assert s.memory_max_context_tokens == 1200


def test_artifact_payload_limit_env_override(monkeypatch):
    s = _fresh_settings(monkeypatch, AGENTKIT_ARTIFACT_MAX_PAYLOAD_BYTES="2048")
    assert s.artifact_max_payload_bytes == 2048


def test_embedding_defaults(monkeypatch):
    s = _fresh_settings(monkeypatch)
    assert s.embedding_provider == "fake"
    assert s.embedding_base_url is None
    assert s.embedding_api_key is None
    assert s.memory_dedup_threshold == 0.92
    assert s.memory_min_retrieval_score == 0.1


def test_rag_defaults(monkeypatch):
    s = _fresh_settings(monkeypatch)
    assert s.rag_enabled is False
    assert s.rag_store_backend == "chroma"
    assert s.rag_chroma_path == "data/chroma"
    assert s.rag_chroma_collection == "agentkit_knowledge"
    assert s.rag_chunk_max_chars == 1200
    assert s.rag_chunk_overlap_chars == 120
    assert s.rag_table_chunk_max_chars == 900
    assert s.rag_ocr_chunk_max_chars == 900
    assert s.rag_keyword_weight == 0.4
    assert s.rag_vector_weight == 0.6
    assert s.rag_query_rewrite == "none"
    assert s.rag_reranker == "none"
    assert s.rag_rerank_candidates == 12
    assert s.rag_top_k == 5
    assert s.rag_context_cap_tokens == 1000
    assert s.rag_ocr_enabled is False


def test_browser_search_env_overrides(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        AGENTKIT_XHS_RESEARCH_PROVIDER="playwright",
        AGENTKIT_XHS_PUBLISH_MEDIA_STRATEGY="xhs_text_image",
        AGENTKIT_XHS_TEXT_IMAGE_STYLE="清新",
        AGENTKIT_XHS_TEXT_IMAGE_GENERATION_TIMEOUT_SECONDS="90",
        AGENTKIT_WEB_SEARCH_HEADLESS="false",
        AGENTKIT_WEB_SEARCH_PROFILE_ROOT="data/test-profiles",
        AGENTKIT_WEB_SEARCH_STORAGE_STATE_ROOT="data/test-browser-state",
    )
    assert s.xhs_research_provider == "playwright"
    assert s.xhs_publish_media_strategy == "xhs_text_image"
    assert s.xhs_text_image_style == "清新"
    assert s.xhs_text_image_generation_timeout_seconds == 90.0
    assert s.web_search_headless is False
    assert s.web_search_profile_root == "data/test-profiles"
    assert s.web_search_storage_state_root == "data/test-browser-state"


def test_get_settings_cached(monkeypatch):
    config_mod.get_settings.cache_clear()
    a = config_mod.get_settings()
    b = config_mod.get_settings()
    assert a is b
