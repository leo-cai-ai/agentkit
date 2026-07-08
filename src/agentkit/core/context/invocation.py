"""统一执行 Context 渲染、模型调用、Schema 校验与审计。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from agentkit.core import llm_client
from agentkit.core.llm_client import strip_reasoning_tags
from agentkit.core.memory.tokenizer import HeuristicTokenEstimator, TokenEstimator

from .assembler import ContextAssembler
from .errors import (
    ContextError,
    ContextOutputInvalidError,
    ContextRenderError,
    ContextTooLargeError,
)
from .models import ContextRenderRequest, LLMInvocationResult, RenderedContext

_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", flags=re.IGNORECASE | re.DOTALL)
_JSON_SCHEMA_HEADING = "Runtime 强制输出契约：必须严格按照以下 JSON Schema 返回唯一 JSON 值。"
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_NAMED_SECRET = re.compile(
    r"(?i)\b(authorization|cookie|token|secret|password)\s*[:=]\s*([^\s,;]+(?:\s+[^\s,;]+)?)"
)


class AuditProtocol(Protocol):
    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None: ...


class ContextDebugSampler:
    """仅用于开发环境的脱敏、限长、自动过期内存采样器。"""

    def __init__(
        self,
        *,
        max_items: int = 20,
        ttl_seconds: float = 300,
        max_chars: int = 2000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_items <= 0 or ttl_seconds <= 0 or max_chars <= 0:
            raise ValueError("Debug Sampler 的容量、TTL 和字符上限必须大于 0")
        self._items: deque[dict[str, Any]] = deque(maxlen=max_items)
        self._ttl_seconds = float(ttl_seconds)
        self._max_chars = int(max_chars)
        self._clock = clock

    def add(self, *, context_id: str, system: str, user: str) -> None:
        self._purge()
        self._items.append(
            {
                "created_at": self._clock(),
                "context_id": context_id,
                "system": _redact_debug_text(system)[: self._max_chars],
                "user": _redact_debug_text(user)[: self._max_chars],
            }
        )

    def items(self) -> list[dict[str, Any]]:
        self._purge()
        return [dict(item) for item in self._items]

    def _purge(self) -> None:
        cutoff = self._clock() - self._ttl_seconds
        while self._items and float(self._items[0]["created_at"]) < cutoff:
            self._items.popleft()


class ContextInvocationService:
    """生产 LLM 节点的唯一上层调用入口。"""

    def __init__(
        self,
        *,
        assembler: ContextAssembler,
        audit: AuditProtocol | None = None,
        call_text: Callable[[str, str], str] | None = None,
        call_stream: Callable[[str, str], str] | None = None,
        tokenizer: TokenEstimator | None = None,
        model_label: str = "configured-model",
        debug_sampler: ContextDebugSampler | None = None,
    ) -> None:
        self._assembler = assembler
        self._audit = audit
        self._call_text = call_text or llm_client.require_chat
        self._call_stream = call_stream or llm_client.require_chat_streaming
        self._tokenizer = tokenizer or HeuristicTokenEstimator()
        self._model_label = model_label
        self._debug_sampler = debug_sampler

    @property
    def manifest_hash(self) -> str:
        return self._assembler.registry.manifest_hash

    def invoke_text(self, request: ContextRenderRequest) -> LLMInvocationResult:
        return self._invoke(
            request,
            call=self._call_text,
            expected_mode="text",
            parse_json=False,
        )

    def invoke_json(self, request: ContextRenderRequest) -> LLMInvocationResult:
        return self._invoke(
            request,
            call=self._call_text,
            expected_mode="json",
            parse_json=True,
        )

    def invoke_streaming(self, request: ContextRenderRequest) -> LLMInvocationResult:
        return self._invoke(
            request,
            call=self._call_stream,
            expected_mode="text",
            parse_json=False,
        )

    def _invoke(
        self,
        request: ContextRenderRequest,
        *,
        call: Callable[[str, str], str],
        expected_mode: str,
        parse_json: bool,
    ) -> LLMInvocationResult:
        rendered: RenderedContext | None = None
        try:
            definition = self._assembler.registry.get(request.context_id)
            if definition.model.output.mode != expected_mode:
                raise ContextRenderError(
                    f"{request.context_id}: 输出模式是 {definition.model.output.mode}，"
                    f"不能通过 {expected_mode} 接口调用",
                    context_id=request.context_id,
                )
            rendered = self._assembler.render(request)
            if parse_json and rendered.output_schema is not None:
                rendered = self._inject_json_schema(request, rendered)
            if self._debug_sampler is not None and definition.model.audit.record_rendered_content:
                self._debug_sampler.add(
                    context_id=request.context_id,
                    system=rendered.system,
                    user=rendered.user,
                )
            raw = call(rendered.system, rendered.user)
            value: Any = _parse_json_value(raw, request.context_id) if parse_json else raw.strip()
            if not parse_json and not value:
                raise ContextOutputInvalidError(
                    f"{request.context_id}: 模型返回空文本",
                    context_id=request.context_id,
                )
            if parse_json and rendered.output_schema is not None:
                errors = sorted(
                    Draft202012Validator(rendered.output_schema).iter_errors(value),
                    key=lambda error: [str(part) for part in error.path],
                )
                if errors:
                    raise ContextOutputInvalidError(
                        f"{request.context_id}: 输出不符合 Schema: {errors[0].message}",
                        context_id=request.context_id,
                    )
            result = LLMInvocationResult(
                value=value,
                rendered=rendered,
                estimated_output_tokens=self._tokenizer.estimate(raw),
            )
            self._record_success(request, result)
            return result
        except Exception as exc:
            self._record_failure(request, rendered, exc)
            raise

    def _inject_json_schema(
        self,
        request: ContextRenderRequest,
        rendered: RenderedContext,
    ) -> RenderedContext:
        schema = json.dumps(
            rendered.output_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        system = f"{rendered.system}\n\n{_JSON_SCHEMA_HEADING}\n{schema}"
        estimated_input_tokens = self._tokenizer.estimate(system) + self._tokenizer.estimate(
            rendered.user
        )
        definition = self._assembler.registry.get(request.context_id)
        effective_limit = min(
            definition.model.limits.max_input_tokens,
            request.global_token_limit - definition.model.limits.response_reserve_tokens,
        )
        if estimated_input_tokens > effective_limit:
            raise ContextTooLargeError(
                f"{request.context_id}: 注入输出 Schema 后超过 {effective_limit} Token",
                context_id=request.context_id,
            )
        return replace(
            rendered,
            system=system,
            estimated_input_tokens=estimated_input_tokens,
        )

    def _record_success(
        self,
        request: ContextRenderRequest,
        result: LLMInvocationResult,
    ) -> None:
        if self._audit is None:
            return
        rendered = result.rendered
        definition = self._assembler.registry.get(request.context_id)
        if rendered.truncation_details:
            self._audit.record(
                request.run_id,
                "context_truncated",
                {
                    "context_id": request.context_id,
                    "details": [dict(item) for item in rendered.truncation_details],
                },
            )
        payload: dict[str, Any] = {
            "context_id": rendered.context_id,
            "context_version": rendered.version,
            "agent_id": str(getattr(request.agent, "name", "")),
            "skill_id": str(getattr(request.skill, "name", "")),
            "estimated_input_tokens": rendered.estimated_input_tokens,
            "estimated_output_tokens": result.estimated_output_tokens,
            "model": self._model_label,
            "status": "succeeded",
        }
        if definition.model.audit.record_input_names:
            payload["included_inputs"] = list(rendered.included_inputs)
            payload["truncated_inputs"] = list(rendered.truncated_inputs)
        if definition.model.audit.record_content_hashes:
            payload["context_hash"] = rendered.content_hash
            payload["override_hash"] = rendered.override_hash
            payload["output_schema_hash"] = _schema_hash(rendered.output_schema)
        self._audit.record(request.run_id, "llm_context", payload)

    def _record_failure(
        self,
        request: ContextRenderRequest,
        rendered: RenderedContext | None,
        error: Exception,
    ) -> None:
        if self._audit is None:
            return
        try:
            definition = self._assembler.registry.get(request.context_id)
        except KeyError:
            definition = None
        payload: dict[str, Any] = {
            "context_id": request.context_id,
            "context_version": definition.model.version if definition else 0,
            "agent_id": str(getattr(request.agent, "name", "")),
            "skill_id": str(getattr(request.skill, "name", "")),
            "model": self._model_label,
            "status": "failed",
            "error_code": error.code if isinstance(error, ContextError) else "llm_call_failed",
            "error_type": type(error).__name__,
        }
        if definition is not None and definition.model.audit.record_content_hashes:
            payload["context_hash"] = definition.content_hash
            payload["override_hash"] = definition.override_hash
            payload["output_schema_hash"] = _schema_hash(definition.output_schema)
        if rendered is not None:
            payload["estimated_input_tokens"] = rendered.estimated_input_tokens
        self._audit.record(request.run_id, "llm_context_failed", payload)


def _parse_json_value(raw: str, context_id: str) -> Any:
    text = strip_reasoning_tags(raw).strip()
    fence = _JSON_FENCE.match(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContextOutputInvalidError(
            f"{context_id}: 模型输出不是合法 JSON",
            context_id=context_id,
        ) from exc


def _schema_hash(schema: dict[str, Any] | None) -> str:
    if schema is None:
        return ""
    encoded = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _redact_debug_text(value: str) -> str:
    text = _NAMED_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    text = _PHONE.sub("[REDACTED_PHONE]", text)
    return _EMAIL.sub("[REDACTED_EMAIL]", text)


__all__ = ["ContextDebugSampler", "ContextInvocationService"]
