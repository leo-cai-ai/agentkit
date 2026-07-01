"""Hardened tool invocation for the executor.

Skills call tools through ``SkillContext.call_tool``, which delegates to a
``ToolExecutor`` so every tool call gets consistent connector-grade governance:

- **Timeout**: each call runs with a per-tool (or global) timeout. Handlers are
  synchronous, so the call runs in a worker thread and the caller bails out on
  timeout (the orphaned thread cannot be force-killed, but the run is unblocked).
- **Retry**: transient failures are retried with exponential backoff, but only
  for calls that are safe to repeat (the tool is marked ``idempotent`` or the
  args carry an ``_idempotency_key``) — never for non-idempotent side effects.
- **Idempotency**: when an ``_idempotency_key`` is supplied, the result is cached
  for the lifetime of the executor (one run), or persisted to an optional durable
  ledger so repeated calls across executors don't re-execute.
- **Audit + tracing**: ``tool_call_started`` / ``tool_call_finished`` /
  ``tool_call_failed`` audit events (with duration, attempts, cached flag) and an
  OpenTelemetry ``tool.call`` span.

Contextvars (run id, usage sink, budget guard, stream sink) are copied into the
worker thread so a tool that itself calls the LLM stays correlated and governed.
"""

from __future__ import annotations

import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Protocol

from .contracts import ToolDefinition
from .idempotency import (
    IdempotencyClaim,
    IdempotencyConflictError,
    IdempotencyError,
    IdempotencyFailedError,
    IdempotencyInProgressError,
    IdempotencyOutcomeUnknownError,
    IdempotencyStore,
    key_digest,
)
from .log_context import current_run_id
from .logging_config import get_logger
from .tracing import span

_log = get_logger("agentkit.tools")
_POOL_LOCK = threading.Lock()
_POOL: ThreadPoolExecutor | None = None
_POOL_WORKERS = 0


def _shared_pool(max_workers: int) -> ThreadPoolExecutor:
    """Return a process-wide bounded pool for synchronous tool handlers."""
    global _POOL, _POOL_WORKERS
    workers = max(1, int(max_workers))
    with _POOL_LOCK:
        if _POOL is None or _POOL_WORKERS != workers:
            old = _POOL
            _POOL = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agentkit-tool")
            _POOL_WORKERS = workers
            if old is not None:
                old.shutdown(wait=False, cancel_futures=True)
        return _POOL


class _Audit(Protocol):
    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None: ...


class ToolExecutionError(RuntimeError):
    """Raised when a tool call fails (after retries) or times out."""


class ToolTimeoutError(ToolExecutionError):
    """Raised when a tool call exceeds its timeout."""


class ToolExecutor:
    """Per-run hardened tool invoker (timeout / retry / idempotency / audit)."""

    def __init__(
        self,
        *,
        tenant_id: str,
        audit: _Audit | None = None,
        run_id: str | None = None,
        timeout_seconds: float = 30.0,
        max_workers: int = 32,
        max_retries: int = 0,
        retry_base_delay: float = 0.2,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._audit = audit
        self._run_id = run_id
        self._timeout = float(timeout_seconds)
        self._max_workers = max(1, int(max_workers))
        self._max_retries = max(0, int(max_retries))
        self._retry_base_delay = float(retry_base_delay)
        self._idempotency_store = idempotency_store
        self._idempotency_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def call(self, tool: ToolDefinition, args: dict[str, Any]) -> dict[str, Any]:
        run_id = self._run_id or current_run_id()
        if (
            self._idempotency_store is not None
            and self._idempotency_store.tenant_id != self._tenant_id
        ):
            raise IdempotencyError("Idempotency store tenant does not match executor tenant")
        idem_key = args.get("_idempotency_key")
        cache_key = (
            (tool.name, str(idem_key)) if self._idempotency_store is None and idem_key else None
        )

        if cache_key is not None and cache_key in self._idempotency_cache:
            self._record(run_id, "tool_call_finished", {"tool": tool.name, "cached": True})
            return self._idempotency_cache[cache_key]

        claim: IdempotencyClaim | None = None
        durable_key = str(idem_key) if self._idempotency_store is not None and idem_key else None
        if durable_key is not None:
            event_payload = {"tool": tool.name, "key_digest": key_digest(durable_key)}
            try:
                claim = self._idempotency_store.begin(
                    tool_name=tool.name,
                    idempotency_key=durable_key,
                    args=args,
                )
            except IdempotencyConflictError:
                self._record(run_id, "idempotency_conflict", event_payload)
                raise
            except IdempotencyInProgressError:
                self._record(run_id, "idempotency_in_progress", event_payload)
                raise
            except IdempotencyFailedError:
                self._record(
                    run_id,
                    "idempotency_failed",
                    {**event_payload, "category": "failed"},
                )
                raise
            except IdempotencyOutcomeUnknownError:
                self._record(run_id, "idempotency_outcome_unknown", event_payload)
                raise

            if not claim.claimed:
                assert claim.result is not None  # guaranteed by the IdempotencyStore contract
                self._record(run_id, "idempotency_cache_hit", event_payload)
                self._record(run_id, "tool_call_finished", {"tool": tool.name, "cached": True})
                return claim.result
            self._record(run_id, "idempotency_claimed", event_payload)

        retryable = bool(tool.idempotent) or (
            self._idempotency_store is None and idem_key is not None
        )
        attempts_allowed = (self._max_retries + 1) if retryable else 1
        timeout = tool.timeout_seconds if tool.timeout_seconds is not None else self._timeout

        self._record(
            run_id,
            "tool_call_started",
            {"tool": tool.name, "retryable": retryable, "timeout_s": timeout},
        )
        started = time.perf_counter()
        last_exc: Exception | None = None

        with span(
            "tool.call", **{"tool.name": tool.name, "tool.idempotent": bool(tool.idempotent)}
        ):
            for attempt in range(attempts_allowed):
                try:
                    result = self._invoke(tool.handler, args, timeout)
                except Exception as exc:  # noqa: BLE001 - normalize to ToolExecutionError
                    last_exc = exc
                    _log.warning(
                        "tool '%s' failed (attempt %d/%d): %s",
                        tool.name,
                        attempt + 1,
                        attempts_allowed,
                        exc,
                    )
                    if attempt + 1 < attempts_allowed:
                        time.sleep(self._retry_base_delay * (2**attempt))
                        continue
                    duration_ms = round((time.perf_counter() - started) * 1000, 3)
                    self._record(
                        run_id,
                        "tool_call_failed",
                        {
                            "tool": tool.name,
                            "attempts": attempt + 1,
                            "duration_ms": duration_ms,
                            "error": str(exc),
                        },
                    )
                    if claim is not None and self._idempotency_store is not None:
                        if isinstance(exc, ToolTimeoutError):
                            try:
                                self._idempotency_store.finish_unknown(claim, str(exc))
                            except Exception:  # noqa: BLE001 - preserve the tool timeout
                                _log.exception("Failed to persist durable idempotency outcome")
                            else:
                                self._record(
                                    run_id,
                                    "idempotency_outcome_unknown",
                                    {
                                        "tool": tool.name,
                                        "key_digest": key_digest(claim.idempotency_key),
                                        "category": "timeout",
                                    },
                                )
                        else:
                            try:
                                self._idempotency_store.finish_failure(claim, str(exc))
                            except Exception:  # noqa: BLE001 - preserve the tool failure
                                _log.exception("Failed to persist durable idempotency outcome")
                    if isinstance(exc, ToolExecutionError):
                        raise
                    raise ToolExecutionError(f"tool '{tool.name}' failed: {exc}") from exc
                else:
                    if claim is not None and self._idempotency_store is not None:
                        try:
                            self._idempotency_store.finish_success(claim, result)
                        except Exception as exc:  # noqa: BLE001 - preserve an unknown outcome
                            try:
                                self._idempotency_store.finish_unknown(
                                    claim,
                                    "persistence_failure",
                                )
                            except Exception:  # noqa: BLE001 - audit unknown even if storage is down
                                _log.exception("Failed to persist durable idempotency outcome")
                            self._record(
                                run_id,
                                "idempotency_outcome_unknown",
                                {
                                    "tool": tool.name,
                                    "key_digest": key_digest(claim.idempotency_key),
                                    "category": "persistence_failure",
                                },
                            )
                            raise IdempotencyOutcomeUnknownError(
                                "Idempotency outcome is unknown after success persistence failed"
                            ) from exc
                    duration_ms = round((time.perf_counter() - started) * 1000, 3)
                    self._record(
                        run_id,
                        "tool_call_finished",
                        {
                            "tool": tool.name,
                            "attempts": attempt + 1,
                            "duration_ms": duration_ms,
                            "cached": False,
                        },
                    )
                    if cache_key is not None and isinstance(result, dict):
                        self._idempotency_cache[cache_key] = result
                    return result

        # Unreachable: the loop always returns or raises.
        raise ToolExecutionError(f"tool '{tool.name}' produced no result: {last_exc}")

    def _invoke(
        self,
        handler: Any,
        args: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        if timeout <= 0:
            return handler(args)
        # Run in a worker thread within a copied context so contextvars (run id,
        # usage sink, budget guard, stream sink) propagate to LLM calls the tool
        # may make. The thread can't be force-killed on timeout, but the run is
        # unblocked and the failure is surfaced.
        ctx = contextvars.copy_context()
        pool = _shared_pool(self._max_workers)
        future = pool.submit(ctx.run, handler, args)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError as exc:
            future.cancel()
            raise ToolTimeoutError(f"tool timed out after {timeout}s") from exc

    def _record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit.record(run_id, event_type, payload)
