"""按 Context Pack 白名单组装受预算约束的 LLM 消息。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agentkit.core.memory.tokenizer import HeuristicTokenEstimator, TokenEstimator

from .errors import ContextInputMissingError, ContextRenderError, ContextTooLargeError
from .models import ContextInputModel, ContextRenderRequest, RenderedContext
from .registry import ContextRegistry
from .sources import ContextSourceRegistry

_TEMPLATE_VARIABLE = re.compile(r"{{\s*([a-z][a-z0-9_]*)\s*}}")
_SENSITIVE_PARTS = (
    "secret",
    "token",
    "password",
    "credential",
    "cookie",
    "authorization",
)
_REDACTED = "[REDACTED]"


@dataclass
class _PreparedInput:
    definition: ContextInputModel
    text: str
    detail: dict[str, int | str] | None = None


class ContextAssembler:
    """选择、脱敏、裁剪并分层渲染某个 LLM 节点的上下文。"""

    def __init__(
        self,
        registry: ContextRegistry,
        *,
        tokenizer: TokenEstimator | None = None,
        sources: ContextSourceRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._tokenizer = tokenizer or HeuristicTokenEstimator()
        self._sources = sources or ContextSourceRegistry.default()

    @property
    def registry(self) -> ContextRegistry:
        return self._registry

    def render(self, request: ContextRenderRequest) -> RenderedContext:
        definition = self._registry.get(request.context_id)
        prepared: list[_PreparedInput] = []
        for input_def in definition.model.inputs:
            if (
                input_def.source not in request.values
                or request.values[input_def.source] is None
            ):
                if input_def.required:
                    raise ContextInputMissingError(request.context_id, input_def.name)
                continue
            prepared.append(
                self._prepare_input(input_def, request.values[input_def.source])
            )

        system = self._render_system(request, definition)
        effective_limit = min(
            definition.model.limits.max_input_tokens,
            request.global_token_limit - definition.model.limits.response_reserve_tokens,
        )
        if effective_limit <= 0:
            raise ContextTooLargeError(
                f"{request.context_id}: 没有可用输入 Token 预算",
                context_id=request.context_id,
            )

        values = {input_def.name: "" for input_def in definition.model.inputs}
        required = [
            prepared_item
            for prepared_item in prepared
            if prepared_item.definition.required
        ]
        optional = sorted(
            (
                prepared_item
                for prepared_item in prepared
                if not prepared_item.definition.required
            ),
            key=lambda prepared_item: (
                -prepared_item.definition.priority,
                prepared_item.definition.name,
            ),
        )
        for prepared_item in required:
            values[prepared_item.definition.name] = prepared_item.text

        user = self._render_user(definition.user_template, values, request.context_id)
        if self._estimate(system, user) > effective_limit:
            raise ContextTooLargeError(
                f"{request.context_id}: 必需上下文超过 {effective_limit} Token",
                context_id=request.context_id,
            )

        included = [prepared_item.definition.name for prepared_item in required]
        budget_truncated: list[str] = []
        budget_details: list[dict[str, int | str]] = []
        for prepared_item in optional:
            candidate_values = dict(values)
            candidate_values[prepared_item.definition.name] = prepared_item.text
            candidate_user = self._render_user(
                definition.user_template,
                candidate_values,
                request.context_id,
            )
            if self._estimate(system, candidate_user) <= effective_limit:
                values = candidate_values
                user = candidate_user
                included.append(prepared_item.definition.name)
                continue
            budget_truncated.append(prepared_item.definition.name)
            budget_details.append(
                {
                    "name": prepared_item.definition.name,
                    "reason": "token_budget",
                    "before_chars": len(prepared_item.text),
                    "after_chars": 0,
                }
            )

        intrinsic_details = [
            prepared_item.detail
            for prepared_item in prepared
            if prepared_item.detail is not None
        ]
        intrinsic_truncated = [
            prepared_item.definition.name
            for prepared_item in prepared
            if prepared_item.detail is not None
        ]
        truncated = tuple(dict.fromkeys((*intrinsic_truncated, *budget_truncated)))
        details = tuple(
            detail for detail in (*intrinsic_details, *budget_details) if detail is not None
        )
        return RenderedContext(
            context_id=definition.model.id,
            version=definition.model.version,
            system=system,
            user=user,
            output_schema=definition.output_schema,
            content_hash=definition.content_hash,
            override_hash=definition.override_hash,
            estimated_input_tokens=self._estimate(system, user),
            included_inputs=tuple(included),
            truncated_inputs=truncated,
            truncation_details=details,
        )

    def _prepare_input(self, item: ContextInputModel, raw: Any) -> _PreparedInput:
        value = _redact(raw)
        detail: dict[str, int | str] | None = None
        if item.max_items is not None and isinstance(value, list | tuple):
            before_items = len(value)
            if before_items > item.max_items:
                value = self._sources.truncate_items(item.truncate, value, item.max_items)
                detail = {
                    "name": item.name,
                    "reason": "max_items",
                    "before_items": before_items,
                    "after_items": len(value),
                }
        text = self._sources.serialize(item.serializer, value)
        if item.max_chars is not None and len(text) > item.max_chars:
            before_chars = len(text)
            text = _truncate_text(text, item.max_chars, item.truncate)
            if detail is None:
                detail = {"name": item.name, "reason": "max_chars"}
            else:
                detail["reason"] = f"{detail['reason']}+max_chars"
            detail["before_chars"] = before_chars
            detail["after_chars"] = len(text)
        return _PreparedInput(definition=item, text=text, detail=detail)

    def _render_system(self, request: ContextRenderRequest, definition: Any) -> str:
        sections = [*definition.fragments, definition.system_template]
        if definition.model.instructions.agent and request.agent is not None:
            instructions = str(getattr(request.agent, "instructions", "")).strip()
            if instructions:
                sections.append(instructions)
        if definition.model.instructions.skill and request.skill is not None:
            instructions = str(getattr(request.skill, "skill_instructions", "")).strip()
            if instructions:
                sections.append(instructions)
        return "\n\n".join(section.strip() for section in sections if section.strip())

    def _render_user(
        self,
        template: str,
        values: dict[str, str],
        context_id: str,
    ) -> str:
        rendered = _TEMPLATE_VARIABLE.sub(lambda match: values[match.group(1)], template)
        if "{{" in rendered or "}}" in rendered:
            raise ContextRenderError(
                f"{context_id}: 模板包含无法解析的变量",
                context_id=context_id,
            )
        return f"UNTRUSTED_DATA_BEGIN\n{rendered.strip()}\nUNTRUSTED_DATA_END"

    def _estimate(self, system: str, user: str) -> int:
        return self._tokenizer.estimate(system) + self._tokenizer.estimate(user)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _REDACTED if _is_sensitive_key(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_PARTS)


def _truncate_text(text: str, limit: int, strategy: str) -> str:
    if limit <= 0:
        return ""
    if strategy in {"tail", "newest"}:
        return text[-limit:]
    return text[:limit]


__all__ = ["ContextAssembler"]
