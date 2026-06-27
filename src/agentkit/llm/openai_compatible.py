"""Generic OpenAI-compatible provider (OpenAI / DeepSeek / local vLLM, etc.)."""

from __future__ import annotations

from agentkit.llm.base import (
    LLMRequiredError,
    estimated_usage,
    extract_text,
    report_usage,
    usage_from_response,
)


class OpenAICompatibleProvider:
    name = "openai"

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str | None,
        model: str | None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not base_url or not api_key or not model:
            raise LLMRequiredError(
                "OpenAI-compatible provider needs AGENTKIT_OPENAI_BASE_URL, "
                "AGENTKIT_OPENAI_API_KEY, and AGENTKIT_OPENAI_MODEL."
            )
        from langchain_openai import ChatOpenAI
        from pydantic import SecretStr

        self._model_name = model
        self._model = ChatOpenAI(
            base_url=base_url,
            api_key=SecretStr(api_key),
            model=model,
            timeout=timeout_seconds,
        )

    def complete(self, system: str, user: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        response = self._model.invoke([SystemMessage(system), HumanMessage(user)])
        text = extract_text(response)
        usage = usage_from_response(response, provider=self.name, model=self._model_name)
        report_usage(
            usage
            or estimated_usage(
                provider=self.name, model=self._model_name, system=system, user=user, output=text
            )
        )
        return text

    def stream(self, system: str, user: str):
        from langchain_core.messages import HumanMessage, SystemMessage

        aggregated = None
        parts: list[str] = []
        for chunk in self._model.stream(
            [SystemMessage(system), HumanMessage(user)], stream_usage=True
        ):
            aggregated = chunk if aggregated is None else aggregated + chunk
            text = extract_text(chunk)
            if text:
                parts.append(text)
                yield text
        usage = usage_from_response(aggregated, provider=self.name, model=self._model_name)
        report_usage(
            usage
            or estimated_usage(
                provider=self.name,
                model=self._model_name,
                system=system,
                user=user,
                output="".join(parts),
            )
        )
