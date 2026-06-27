"""LLM provider abstraction, shared error, and response text extraction."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class LLMRequiredError(RuntimeError):
    """Raised when an LLM-required runtime step cannot complete."""


@dataclass(frozen=True)
class LLMUsage:
    """Token usage for a single LLM call.

    ``estimated`` is True when the provider could not return real token counts
    (e.g. a streamed response without usage metadata, or the fake provider) and
    the numbers come from a heuristic instead.
    """

    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated: bool = False


# Usage sink for the current execution context. ``core.cost.CostTracker`` binds a
# callback here; providers call ``report_usage`` after each call so cost/token
# accounting works without changing provider return types or threading an audit
# object through every call site. No sink bound -> reporting is a no-op.
_usage_sink: ContextVar[Callable[[LLMUsage], None] | None] = ContextVar(
    "agentkit_llm_usage_sink", default=None
)


@contextmanager
def usage_sink(sink: Callable[[LLMUsage], None] | None) -> Iterator[None]:
    """Bind a usage sink for the duration of the block (and reset after)."""
    token = _usage_sink.set(sink)
    try:
        yield
    finally:
        _usage_sink.reset(token)


def report_usage(usage: LLMUsage | None) -> None:
    """Forward token usage to the active sink, if any (no-op otherwise)."""
    if usage is None:
        return
    sink = _usage_sink.get()
    if sink is not None:
        sink(usage)


def estimate_tokens(text: str) -> int:
    """Rough heuristic token count (~4 chars/token) for fallback accounting."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def usage_from_response(response: Any, *, provider: str, model: str) -> LLMUsage | None:
    """Build :class:`LLMUsage` from a LangChain response's ``usage_metadata``.

    Returns ``None`` when the response carries no usage metadata so the caller
    can fall back to an estimate.
    """
    meta = getattr(response, "usage_metadata", None)
    if not isinstance(meta, dict):
        return None
    input_tokens = int(meta.get("input_tokens", 0) or 0)
    output_tokens = int(meta.get("output_tokens", 0) or 0)
    total_tokens = int(meta.get("total_tokens", input_tokens + output_tokens) or 0)
    if total_tokens <= 0:
        return None
    return LLMUsage(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated=False,
    )


def estimated_usage(*, provider: str, model: str, system: str, user: str, output: str) -> LLMUsage:
    """Heuristic usage when the provider returns no real token counts."""
    input_tokens = estimate_tokens(system) + estimate_tokens(user)
    output_tokens = estimate_tokens(output)
    return LLMUsage(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        estimated=True,
    )


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, user: str) -> str:
        """Single-shot completion: system + user prompt -> text reply."""
        ...

    def stream(self, system: str, user: str) -> Iterator[str]:
        """Stream the completion as text chunks (concatenate to the full reply).

        Providers should yield incremental deltas as the model produces them so
        callers can surface tokens live. A provider may fall back to yielding the
        whole reply as a single chunk if it cannot stream.
        """
        ...


def extract_text(response: Any) -> str:
    """Pull plain text out of a LangChain-style response message."""
    content = getattr(response, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        content = "".join(parts)
    return str(content or "")
