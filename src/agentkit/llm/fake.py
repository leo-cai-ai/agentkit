"""Deterministic, scriptable provider for tests (no network/credentials)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from agentkit.llm.base import LLMRequiredError, estimated_usage, report_usage


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        *,
        responder: Callable[[str, str], str] | None = None,
        responses: list[str] | None = None,
    ) -> None:
        self._responder = responder
        self._responses = list(responses) if responses is not None else None

    def complete(self, system: str, user: str) -> str:
        text = self._reply(system, user)
        report_usage(
            estimated_usage(provider=self.name, model="fake", system=system, user=user, output=text)
        )
        return text

    def _reply(self, system: str, user: str) -> str:
        if self._responder is not None:
            return self._responder(system, user)
        if self._responses is not None:
            if not self._responses:
                raise LLMRequiredError("FakeProvider response queue exhausted.")
            return self._responses.pop(0)
        return "ok"

    def stream(self, system: str, user: str) -> Iterator[str]:
        # Deterministic chunking so tests can assert streaming while
        # "".join(chunks) still equals complete().
        text = self.complete(system, user)
        for i in range(0, len(text), 8):
            yield text[i : i + 8]
