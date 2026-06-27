"""Build the configured LLM provider."""

from __future__ import annotations

import json
from typing import Any

from agentkit.config import Settings
from agentkit.llm.base import LLMProvider, LLMRequiredError


def _build_openai_extra_body(settings: Settings) -> dict[str, Any] | None:
    """Assemble the OpenAI extra_body from the disable-thinking flag + raw JSON.

    The convenience flag seeds ``chat_template_kwargs.enable_thinking = false``;
    any ``openai_extra_body`` JSON is then merged on top (with a nested merge for
    ``chat_template_kwargs`` so both can coexist). Returns None when empty.
    """
    extra: dict[str, Any] = {}
    if settings.openai_disable_thinking:
        extra["chat_template_kwargs"] = {"enable_thinking": False}

    raw = settings.openai_extra_body.strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMRequiredError(
                f"AGENTKIT_OPENAI_EXTRA_BODY is not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise LLMRequiredError("AGENTKIT_OPENAI_EXTRA_BODY must be a JSON object.")
        for key, value in parsed.items():
            if (
                key == "chat_template_kwargs"
                and isinstance(value, dict)
                and isinstance(extra.get(key), dict)
            ):
                extra[key].update(value)
            else:
                extra[key] = value

    return extra or None


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
            max_tokens=settings.llm_max_tokens,
            extra_body=_build_openai_extra_body(settings),
        )
    if provider == "fake":
        from agentkit.llm.fake import FakeProvider

        return FakeProvider()
    raise LLMRequiredError(f"Unknown llm_provider: {provider}")
