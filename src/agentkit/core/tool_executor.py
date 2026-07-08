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
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Protocol

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json

from .contracts import ToolDefinition
from .execution.models import ToolPolicy, ToolProvider, ToolRisk
from .idempotency import (
    IdempotencyClaim,
    IdempotencyConflictError,
    IdempotencyError,
    IdempotencyFailedError,
    IdempotencyInProgressError,
    IdempotencyOutcomeUnknownError,
    IdempotencyStore,
    canonical_args_hash,
    key_digest,
)
from .log_context import current_run_id
from .logging_config import get_logger
from .tool_backends import PythonToolBackend, ToolBackendRegistry
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


class ToolSafeFailureError(ToolExecutionError):
    """Raised only when a side effect is known not to have been submitted."""


class ToolTimeoutError(ToolExecutionError):
    """Raised when a tool call exceeds its timeout."""


class ToolPermissionError(ToolExecutionError):
    """当前用户或 Agent 缺少 Tool 权限。"""


class ToolRiskError(ToolExecutionError):
    """Tool 风险超出当前执行策略。"""


class ToolSchemaError(ToolExecutionError):
    """Tool 输入不符合声明的 JSON Schema。"""


def safe_tool_error_message(exc: BaseException, args: Mapping[str, Any]) -> str:
    """Format a tool error without exposing idempotency transport values."""
    message = str(exc)
    for key in ("_idempotency_key", "idempotency_key"):
        value = args.get(key)
        if value is not None:
            raw_value = str(value)
            if raw_value:
                message = message.replace(raw_value, "[REDACTED]")
    return message


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
        backends: ToolBackendRegistry | None = None,
        permissions: set[str] | None = None,
        allowed_tools: set[str] | None = None,
        tool_policy: ToolPolicy = ToolPolicy.GOVERNED,
        approved_side_effects: set[str] | None = None,
        action_tool_idempotency_key: str = "",
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
        self._backends = backends or ToolBackendRegistry({ToolProvider.PYTHON: PythonToolBackend()})
        self._permissions = set(permissions or ())
        self._allowed_tools = None if allowed_tools is None else set(allowed_tools)
        self._tool_policy = tool_policy
        self._approved_side_effects = set(approved_side_effects or ())
        self._action_tool_idempotency_key = str(action_tool_idempotency_key or "")

    def call(self, tool: ToolDefinition, args: dict[str, Any]) -> dict[str, Any]:
        args = self._apply_action_idempotency(tool, args)
        self._validate_access(tool, args)
        run_id = self._run_id or current_run_id()
        idempotency_store = self._idempotency_store
        if idempotency_store is not None and idempotency_store.tenant_id != self._tenant_id:
            raise IdempotencyError("Idempotency store tenant does not match executor tenant")
        idem_key = args.get("_idempotency_key")
        cache_key = (tool.name, str(idem_key)) if idempotency_store is None and idem_key else None

        if cache_key is not None and cache_key in self._idempotency_cache:
            self._record(run_id, "tool_call_finished", {"tool": tool.name, "cached": True})
            return self._idempotency_cache[cache_key]

        claim: IdempotencyClaim | None = None
        durable_key: str | None = None
        if idempotency_store is not None and idem_key:
            durable_key = str(idem_key)
            event_payload = {"tool": tool.name, "key_digest": key_digest(durable_key)}
            try:
                claim = idempotency_store.begin(
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

        retryable = bool(tool.idempotent) or (idempotency_store is None and idem_key is not None)
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
                    result = self._invoke(tool, args, timeout)
                except Exception as exc:  # noqa: BLE001 - normalize to ToolExecutionError
                    last_exc = exc
                    safe_error = safe_tool_error_message(exc, args)
                    _log.warning(
                        "tool '%s' failed (attempt %d/%d): %s",
                        tool.name,
                        attempt + 1,
                        attempts_allowed,
                        safe_error,
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
                            "error": safe_error,
                        },
                    )
                    if claim is not None and idempotency_store is not None:
                        if isinstance(exc, ToolSafeFailureError):
                            try:
                                idempotency_store.finish_failure(claim, safe_error)
                            except Exception:  # noqa: BLE001 - preserve the original tool error
                                _log.exception("Failed to persist durable idempotency outcome")
                        else:
                            try:
                                idempotency_store.finish_unknown(claim, safe_error)
                            except Exception:  # noqa: BLE001 - preserve the original tool error
                                _log.exception("Failed to persist durable idempotency outcome")
                            else:
                                self._record(
                                    run_id,
                                    "idempotency_outcome_unknown",
                                    {
                                        "tool": tool.name,
                                        "key_digest": key_digest(claim.idempotency_key),
                                        "category": (
                                            "timeout"
                                            if isinstance(exc, ToolTimeoutError)
                                            else "unconfirmed_failure"
                                        ),
                                    },
                                )
                    if isinstance(exc, ToolExecutionError):
                        raise
                    raise ToolExecutionError(f"tool '{tool.name}' failed: {safe_error}") from exc
                else:
                    if claim is not None and idempotency_store is not None:
                        try:
                            idempotency_store.finish_success(claim, result)
                        except Exception as exc:  # noqa: BLE001 - preserve an unknown outcome
                            try:
                                idempotency_store.finish_unknown(
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

    def _apply_action_idempotency(
        self,
        tool: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if tool.risk is not ToolRisk.SIDE_EFFECT or not self._action_tool_idempotency_key:
            return args
        resolved = dict(args)
        generated = (
            f"{self._action_tool_idempotency_key}:{tool.name}:"
            f"{canonical_args_hash(resolved)[:16]}"
        )
        # The server-derived Action key is authoritative during approval resume.
        # Legacy Skill-authored keys remain valid outside that trusted context.
        resolved["_idempotency_key"] = generated
        return resolved

    def _invoke(
        self,
        tool: ToolDefinition,
        args: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        backend = self._backends.get(tool.provider)
        if timeout <= 0:
            return backend.execute(tool, args)
        # Run in a worker thread within a copied context so contextvars (run id,
        # usage sink, budget guard, stream sink) propagate to LLM calls the tool
        # may make. The thread can't be force-killed on timeout, but the run is
        # unblocked and the failure is surfaced.
        ctx = contextvars.copy_context()
        pool = _shared_pool(self._max_workers)
        future = pool.submit(ctx.run, backend.execute, tool, args)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError as exc:
            future.cancel()
            raise ToolTimeoutError(f"tool timed out after {timeout}s") from exc

    def _validate_access(self, tool: ToolDefinition, args: dict[str, Any]) -> None:
        if self._allowed_tools is not None and tool.name not in self._allowed_tools:
            raise ToolPermissionError(f"Tool 不在当前 Skill 白名单中: {tool.name}")
        missing = sorted(set(tool.permissions) - self._permissions)
        if missing:
            raise ToolPermissionError(f"缺少 Tool 权限: {', '.join(missing)}")
        if self._tool_policy is ToolPolicy.NONE:
            raise ToolRiskError("当前策略不允许调用 Tool")
        if self._tool_policy is ToolPolicy.READ_ONLY and tool.risk is not ToolRisk.READ_ONLY:
            raise ToolRiskError(f"只读策略不能调用 {tool.risk.value} Tool: {tool.name}")
        if self._tool_policy is ToolPolicy.GOVERNED and tool.risk is ToolRisk.SIDE_EFFECT:
            raise ToolRiskError(f"governed 策略不能直接执行副作用 Tool: {tool.name}")
        if tool.risk is ToolRisk.SIDE_EFFECT and tool.name not in self._approved_side_effects:
            raise ToolRiskError(f"副作用 Tool 尚未获得审批: {tool.name}")
        public_args = {key: value for key, value in args.items() if not key.startswith("_")}
        try:
            validate_json(instance=public_args, schema=tool.input_schema)
        except JsonSchemaValidationError as exc:
            raise ToolSchemaError(f"Tool 输入 Schema 校验失败: {exc.message}") from exc

    def _record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit.record(run_id, event_type, payload)
