"""LLM client helpers around the configured provider (see agentkit.llm.factory).

``chat`` / ``chat_json`` keep the older optional behavior (return None on failure).
The runtime's agent path uses ``require_chat`` / ``require_chat_json``: they fail
loudly (LLMRequiredError) when the provider is unavailable, the call keeps failing
after retries, or the response cannot be parsed.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Any

from agentkit.llm.base import LLMRequiredError

from .logging_config import get_logger

_log = get_logger("agentkit.llm")

# User-facing fallback when a reply is cut off mid-output (commonly a reasoning
# model whose <think> block exhausts the completion budget before the answer or
# JSON is finished). Shown instead of a raw parse error / truncated dump.
TRUNCATED_RESPONSE_MESSAGE = "结果超长，已被截取，请联系工作人员。"

# Active streaming sink for the current execution context. When set (e.g. by an
# SSE endpoint running the agent in a worker thread), ``require_chat_streaming``
# pushes each text chunk to it as the model produces it. Only the user-facing
# final-answer calls stream; governance/JSON nodes stay on the blocking path.
_stream_sink: ContextVar[Callable[[str], None] | None] = ContextVar(
    "agentkit_stream_sink", default=None
)

# Optional budget guard for the current execution context. ``core.cost.CostTracker``
# binds a callable here; every user-facing LLM call invokes it first and the
# guard raises when the run's accumulated cost exceeds the configured cap.
_budget_guard: ContextVar[Callable[[], None] | None] = ContextVar(
    "agentkit_budget_guard", default=None
)

__all__ = [
    "LLMRequiredError",
    "llm_available",
    "require_model",
    "require_chat",
    "require_chat_json",
    "require_chat_streaming",
    "stream_sink",
    "budget_guard",
    "enforce_budget",
    "clear_provider_cache",
    "chat",
    "chat_json",
]


@contextmanager
def stream_sink(sink: Callable[[str], None] | None) -> Iterator[None]:
    """Bind a streaming sink for the duration of the block (and reset after).

    Tokens from ``require_chat_streaming`` are forwarded to ``sink`` while
    active. ``None`` clears any inherited sink.
    """
    token = _stream_sink.set(sink)
    try:
        yield
    finally:
        _stream_sink.reset(token)


@contextmanager
def budget_guard(guard: Callable[[], None] | None) -> Iterator[None]:
    """Bind a budget guard for the duration of the block (and reset after)."""
    token = _budget_guard.set(guard)
    try:
        yield
    finally:
        _budget_guard.reset(token)


def enforce_budget() -> None:
    """Invoke the active budget guard (no-op when none is bound)."""
    guard = _budget_guard.get()
    if guard is not None:
        guard()


@lru_cache(maxsize=1)
def _get_provider():
    from agentkit.config import get_settings
    from agentkit.llm.factory import build_provider

    return build_provider(get_settings())


def clear_provider_cache() -> None:
    """Drop the cached provider so the next call rebuilds from current settings."""
    cache_clear = getattr(_get_provider, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()


def llm_available() -> bool:
    try:
        _get_provider()
        return True
    except Exception:
        return False


def require_model():
    """Return the configured provider, raising when no provider is available."""
    try:
        return _get_provider()
    except LLMRequiredError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize to the runtime's error type
        raise LLMRequiredError(f"LLM provider unavailable: {exc}") from exc


def chat(system: str, user: str) -> str | None:
    try:
        return require_chat(system, user)
    except LLMRequiredError:
        return None


def require_chat(system: str, user: str) -> str:
    from agentkit.config import get_settings

    from .tracing import span

    enforce_budget()
    settings = get_settings()
    provider = require_model()
    with span("llm.complete", **{"llm.provider": getattr(provider, "name", "?")}):
        for attempt in range(settings.llm_max_retries + 1):
            try:
                text = provider.complete(system, user)
            except Exception as exc:  # noqa: BLE001
                _log.warning("LLM call failed (attempt %d): %s", attempt + 1, exc)
                if attempt < settings.llm_max_retries:
                    time.sleep(settings.llm_retry_base_delay * (2**attempt))
                    continue
                raise LLMRequiredError(f"LLM call failed after retries: {exc}") from exc
            if not text:
                raise LLMRequiredError("LLM returned an empty response.")
            return str(text).strip()
    raise LLMRequiredError("LLM produced no response.")


def require_chat_streaming(system: str, user: str) -> str:
    """Stream a user-facing reply, forwarding chunks to the active sink.

    Behaves exactly like :func:`require_chat` for callers that only need the
    final string (it accumulates and returns the full text). When a sink is
    bound via :func:`stream_sink`, each chunk is pushed as the model emits it,
    enabling token-by-token delivery to the UI. Falls back to a single chunk
    when the provider cannot stream.
    """
    from agentkit.config import get_settings

    from .tracing import span

    enforce_budget()
    settings = get_settings()
    provider = require_model()
    sink = _stream_sink.get()
    streamer = getattr(provider, "stream", None)

    if streamer is None:
        text = require_chat(system, user)
        if sink is not None:
            sink(text)
        return text

    with span("llm.stream", **{"llm.provider": getattr(provider, "name", "?")}):
        for attempt in range(settings.llm_max_retries + 1):
            parts: list[str] = []
            try:
                for chunk in streamer(system, user):
                    if not chunk:
                        continue
                    parts.append(chunk)
                    if sink is not None:
                        sink(chunk)
            except Exception as exc:  # noqa: BLE001
                if parts:
                    # Already streamed partial output; retrying would duplicate
                    # chunks in the sink, so surface the failure instead.
                    raise LLMRequiredError(f"LLM stream failed mid-response: {exc}") from exc
                _log.warning("LLM stream failed (attempt %d): %s", attempt + 1, exc)
                if attempt < settings.llm_max_retries:
                    time.sleep(settings.llm_retry_base_delay * (2**attempt))
                    continue
                raise LLMRequiredError(f"LLM stream failed after retries: {exc}") from exc
            text = "".join(parts).strip()
            if not text:
                raise LLMRequiredError("LLM returned an empty response.")
            # If the answer was cut off mid-thought, append a graceful note (and
            # stream it) instead of leaving the user with a dangling fragment.
            if _has_unclosed_think(text):
                _log.warning("Streamed answer truncated mid-<think>; appending fallback note.")
                if sink is not None:
                    sink("\n\n" + TRUNCATED_RESPONSE_MESSAGE)
                text = f"{text}\n\n{TRUNCATED_RESPONSE_MESSAGE}"
            return text
    raise LLMRequiredError("LLM produced no response.")


def chat_json(system: str, user: str) -> dict[str, Any] | None:
    try:
        return require_chat_json(system, user)
    except LLMRequiredError:
        return None


def require_chat_json(system: str, user: str) -> dict[str, Any]:
    raw = require_chat(system, user)
    data = _extract_json(raw)
    if data is None:
        # A truncated reply has no parseable JSON. Surface a clean fallback
        # message rather than dumping the cut-off raw text at the user.
        if _looks_truncated(raw):
            _log.warning("LLM response truncated; no JSON parsed (len=%d).", len(raw))
            raise LLMRequiredError(TRUNCATED_RESPONSE_MESSAGE)
        raise LLMRequiredError(f"LLM did not return a valid JSON object: {raw[:500]}")
    return data


def _has_unclosed_think(raw: str) -> bool:
    """True when more ``<think>`` opens than ``</think>`` closes (cut mid-thought)."""
    opens = len(re.findall(r"<think\b", raw, flags=re.I))
    closes = len(re.findall(r"</think\s*>", raw, flags=re.I))
    return opens > closes


def _looks_truncated(raw: str) -> bool:
    """Heuristic: the response was cut off before completing.

    Two strong signals: an unclosed ``<think>`` block (reasoning model ran out
    of budget mid-thought) or an unbalanced ``{`` count (JSON cut mid-object).
    """
    return _has_unclosed_think(raw) or raw.count("{") > raw.count("}")


def _strip_think(raw: str) -> str:
    """Remove reasoning-model ``<think>...</think>`` blocks from a response.

    Models like DeepSeek-R1 / Qwen emit a chain-of-thought wrapped in
    ``<think>`` tags before the actual answer. That prose routinely contains
    ``{`` / ``}`` characters, which makes the brace-matching fallback in
    :func:`_extract_json` capture the thinking text and fail to parse. Strip
    complete blocks first, then any dangling/unbalanced markers left behind by
    truncated output.
    """
    cleaned = re.sub(r"<think\b[^>]*>.*?</think>", "", raw, flags=re.S | re.I)
    # Unclosed opening tag (stream cut off before the answer/JSON began).
    cleaned = re.sub(r"<think\b[^>]*>.*$", "", cleaned, flags=re.S | re.I)
    # Stray closing tag with no matching open (the open block was dropped).
    cleaned = re.sub(r"^.*?</think>", "", cleaned, flags=re.S | re.I)
    return cleaned


def _extract_json(raw: str) -> dict[str, Any] | None:
    text = _strip_think(raw).strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                data, _end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return None
