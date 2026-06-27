"""Build the configured LLM provider."""

from __future__ import annotations

from agentkit.config import Settings
from agentkit.llm.base import LLMProvider, LLMRequiredError


def build_provider(settings: Settings) -> LLMProvider:
    """Build the configured provider, wrapping it in failover when fallbacks exist."""
    primary = _build_single(settings.llm_provider, settings)

    fallbacks = [
        name.strip() for name in settings.llm_fallback_providers.split(",") if name.strip()
    ]
    if not fallbacks:
        return primary

    providers: list[LLMProvider] = [primary]
    for name in fallbacks:
        if name == settings.llm_provider:
            continue  # avoid duplicating the primary
        try:
            providers.append(_build_single(name, settings))
        except LLMRequiredError:
            continue  # skip unbuildable fallbacks rather than failing startup
    if len(providers) == 1:
        return primary

    from agentkit.llm.resilient import FailoverProvider

    return FailoverProvider(
        providers,
        failure_threshold=settings.llm_circuit_failure_threshold,
        reset_timeout=settings.llm_circuit_reset_seconds,
    )


def _build_single(provider: str, settings: Settings) -> LLMProvider:
    if provider == "customer_band":
        from agentkit.llm.customer_band import CustomerBandProvider
        from agentkit.llm.rate_limit import build_rate_limiter

        return CustomerBandProvider(
            client_id=settings.ai_client_id,
            client_secret=(
                settings.ai_client_secret.get_secret_value() if settings.ai_client_secret else None
            ),
            app_key=(settings.ai_app_key.get_secret_value() if settings.ai_app_key else None),
            timeout_seconds=settings.llm_timeout_seconds,
            rate_limiter=build_rate_limiter(settings),
        )
    if provider == "openai":
        from agentkit.llm.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            base_url=settings.openai_base_url,
            api_key=(
                settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
            ),
            model=settings.openai_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    if provider == "fake":
        from agentkit.llm.fake import FakeProvider

        return FakeProvider()
    raise LLMRequiredError(f"Unknown llm_provider: {provider}")
