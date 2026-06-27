"""Unit tests for LLM failover + circuit breaker (agentkit.llm.resilient)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agentkit.llm.base import LLMRequiredError
from agentkit.llm.resilient import CircuitBreaker, FailoverProvider


class Good:
    def __init__(self, name: str, text: str = "ok", chunks: list[str] | None = None) -> None:
        self.name = name
        self._text = text
        self._chunks = chunks
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self._text

    def stream(self, system: str, user: str) -> Iterator[str]:
        self.calls += 1
        yield from (self._chunks if self._chunks is not None else [self._text])


class Bad:
    def __init__(self, name: str = "bad") -> None:
        self.name = name
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        raise RuntimeError("boom")

    def stream(self, system: str, user: str) -> Iterator[str]:
        self.calls += 1
        raise RuntimeError("boom")
        yield ""  # unreachable; makes this a generator


class NoStream:
    name = "nostream"

    def complete(self, system: str, user: str) -> str:
        return "from-complete"


# --- circuit breaker -------------------------------------------------------- #


def test_breaker_opens_after_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=2, reset_timeout=60)
    assert cb.allow() is True
    cb.record_failure()
    assert cb.state == "closed"
    cb.record_failure()
    assert cb.state == "open"
    assert cb.allow() is False


def test_breaker_success_resets() -> None:
    cb = CircuitBreaker(failure_threshold=1, reset_timeout=60)
    cb.record_failure()
    assert cb.allow() is False
    cb.record_success()
    assert cb.allow() is True
    assert cb.state == "closed"


def test_breaker_half_open_after_cooldown() -> None:
    cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.0)
    cb.record_failure()
    # reset_timeout 0 -> immediately half-open (a trial is allowed).
    assert cb.allow() is True
    assert cb.state == "half_open"


# --- failover: complete ----------------------------------------------------- #


def test_complete_fails_over_to_second() -> None:
    bad, good = Bad(), Good("good", text="recovered")
    fp = FailoverProvider([bad, good])
    assert fp.complete("s", "u") == "recovered"
    assert bad.calls == 1 and good.calls == 1


def test_complete_empty_counts_as_failure() -> None:
    empty, good = Good("empty", text=""), Good("good", text="real")
    fp = FailoverProvider([empty, good])
    assert fp.complete("s", "u") == "real"


def test_complete_all_fail_raises() -> None:
    fp = FailoverProvider([Bad("b1"), Bad("b2")])
    with pytest.raises(LLMRequiredError) as exc:
        fp.complete("s", "u")
    assert "all LLM providers failed" in str(exc.value)


def test_open_breaker_skips_provider() -> None:
    bad, good = Bad(), Good("good")
    fp = FailoverProvider([bad, good], failure_threshold=1, reset_timeout=60)
    fp.complete("s", "u")  # bad fails once -> breaker opens
    fp.complete("s", "u")  # bad skipped this time
    assert bad.calls == 1  # not retried while open
    assert good.calls == 2


# --- failover: stream ------------------------------------------------------- #


def test_stream_fails_over_before_first_chunk() -> None:
    bad, good = Bad(), Good("good", chunks=["a", "b"])
    fp = FailoverProvider([bad, good])
    assert "".join(fp.stream("s", "u")) == "ab"


def test_stream_uses_complete_when_no_stream_method() -> None:
    fp = FailoverProvider([NoStream()])
    assert "".join(fp.stream("s", "u")) == "from-complete"


def test_stream_all_fail_raises() -> None:
    fp = FailoverProvider([Bad("b1"), Bad("b2")])
    with pytest.raises(LLMRequiredError):
        list(fp.stream("s", "u"))


def test_empty_provider_list_rejected() -> None:
    with pytest.raises(ValueError):
        FailoverProvider([])
