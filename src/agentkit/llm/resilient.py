"""Multi-provider failover with per-provider circuit breakers.

``FailoverProvider`` wraps an ordered list of :class:`LLMProvider` and presents
the same interface. Each call tries providers in order, skipping any whose
circuit breaker is open, and fails over on error/empty output. Breakers open
after N consecutive failures and half-open after a cooldown so a recovered
provider is retried without hammering a dead one.

Streaming fails over only *before the first chunk*: once tokens have been
delivered to the caller, a mid-stream error is surfaced rather than restarted
(restarting would duplicate already-emitted output).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

from agentkit.llm.base import LLMProvider, LLMRequiredError

from ..core.logging_config import get_logger

_log = get_logger("agentkit.llm.resilient")


class CircuitBreaker:
    """A simple thread-safe circuit breaker (closed -> open -> half-open)."""

    def __init__(self, *, failure_threshold: int = 3, reset_timeout: float = 30.0) -> None:
        self._threshold = max(1, failure_threshold)
        self._reset_timeout = max(0.0, reset_timeout)
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until: float | None = None

    def allow(self) -> bool:
        """True if a call may proceed (closed, or half-open trial after cooldown)."""
        with self._lock:
            if self._open_until is None:
                return True
            if time.monotonic() >= self._open_until:
                # Half-open: permit a single trial; success/failure updates state.
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._open_until = time.monotonic() + self._reset_timeout

    @property
    def state(self) -> str:
        with self._lock:
            if self._open_until is None:
                return "closed"
            if time.monotonic() >= self._open_until:
                return "half_open"
            return "open"


class FailoverProvider:
    """An :class:`LLMProvider` that fails over across an ordered provider list."""

    name = "failover"

    def __init__(
        self,
        providers: list[LLMProvider],
        *,
        failure_threshold: int = 3,
        reset_timeout: float = 30.0,
    ) -> None:
        if not providers:
            raise ValueError("FailoverProvider requires at least one provider")
        self._providers = providers
        self._breakers = [
            CircuitBreaker(failure_threshold=failure_threshold, reset_timeout=reset_timeout)
            for _ in providers
        ]

    @property
    def providers(self) -> list[LLMProvider]:
        return self._providers

    def complete(self, system: str, user: str) -> str:
        errors: list[str] = []
        for provider, breaker in zip(self._providers, self._breakers, strict=True):
            pname = getattr(provider, "name", "?")
            if not breaker.allow():
                errors.append(f"{pname}: circuit open")
                continue
            try:
                text = provider.complete(system, user)
            except Exception as exc:  # noqa: BLE001 - any failure triggers failover
                breaker.record_failure()
                _log.warning("provider %s failed; failing over: %s", pname, exc)
                errors.append(f"{pname}: {exc}")
                continue
            if not text:
                breaker.record_failure()
                errors.append(f"{pname}: empty response")
                continue
            breaker.record_success()
            return text
        raise LLMRequiredError("all LLM providers failed: " + "; ".join(errors))

    def stream(self, system: str, user: str) -> Iterator[str]:
        errors: list[str] = []
        for provider, breaker in zip(self._providers, self._breakers, strict=True):
            pname = getattr(provider, "name", "?")
            if not breaker.allow():
                errors.append(f"{pname}: circuit open")
                continue

            streamer = getattr(provider, "stream", None)
            if streamer is None:
                try:
                    text = provider.complete(system, user)
                except Exception as exc:  # noqa: BLE001
                    breaker.record_failure()
                    errors.append(f"{pname}: {exc}")
                    continue
                if not text:
                    breaker.record_failure()
                    errors.append(f"{pname}: empty response")
                    continue
                breaker.record_success()
                yield text
                return

            try:
                iterator = streamer(system, user)
                first = self._first_nonempty(iterator)
            except Exception as exc:  # noqa: BLE001 - pre-first-chunk failure is failoverable
                breaker.record_failure()
                _log.warning("provider %s stream failed; failing over: %s", pname, exc)
                errors.append(f"{pname}: {exc}")
                continue
            if first is None:
                breaker.record_failure()
                errors.append(f"{pname}: empty stream")
                continue
            breaker.record_success()
            yield first
            # Past the first chunk we cannot fail over without duplicating output;
            # any error here propagates to the caller.
            yield from iterator
            return
        raise LLMRequiredError("all LLM providers failed (stream): " + "; ".join(errors))

    @staticmethod
    def _first_nonempty(iterator: Iterator[str]) -> str | None:
        for chunk in iterator:
            if chunk:
                return chunk
        return None


__all__ = ["CircuitBreaker", "FailoverProvider"]
