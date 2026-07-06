"""按已选 Skill Schema 统一补全和验证输入参数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsonschema import Draft7Validator

from .context.models import ContextRenderRequest
from .contracts import AgentProfile, SkillDefinition, TaskRequest
from .llm_client import LLMRequiredError
from .schema_validation import SkillInputError

_CONFIDENCE = frozenset({"high", "medium", "low"})


@dataclass(frozen=True)
class InputResolution:
    arguments: dict[str, Any]
    missing: tuple[str, ...]
    clarification: str = ""
    confidence: str = "high"
    llm_used: bool = False


class SchemaInputResolver:
    """只在必填参数缺失时调用 LLM，并拒绝未经 Schema 验证的值。"""

    def __init__(
        self,
        *,
        context_invoker: Any,
        tenant_id: str,
        tenant_selector: str,
    ) -> None:
        self._context_invoker = context_invoker
        self._tenant_id = tenant_id
        self._tenant_selector = tenant_selector

    def resolve(
        self,
        request: TaskRequest,
        *,
        agent: AgentProfile,
        skill: SkillDefinition,
        arguments: dict[str, Any],
        run_id: str,
    ) -> InputResolution:
        schema = skill.input_schema or {"type": "object"}
        resolved = dict(arguments)
        missing = self._missing_required(schema, resolved)
        if not missing:
            self._validate_complete(skill=skill, schema=schema, arguments=resolved)
            return InputResolution(arguments=resolved, missing=())

        agent_context = request.context.get("agent_context")
        summary = agent_context.get("summary", "") if isinstance(agent_context, dict) else ""
        result = self._context_invoker.invoke_json(
            ContextRenderRequest(
                context_id="runtime.input-resolve",
                tenant_id=self._tenant_id,
                tenant_selector=self._tenant_selector,
                run_id=run_id,
                agent=agent,
                skill=skill,
                values={
                    "request.message": request.text,
                    "conversation.summary": str(summary or ""),
                    "request.arguments": resolved,
                    "skill.missing_fields": list(missing),
                    "skill.input_schema": schema,
                },
                global_token_limit=min(agent.max_tokens, agent.autonomy_budget.max_tokens),
            )
        ).value
        if not isinstance(result, dict):
            raise LLMRequiredError("Input resolution LLM 必须返回对象")

        properties = schema.get("properties", {})
        candidates = result.get("resolved")
        if isinstance(properties, dict) and isinstance(candidates, dict):
            for name in missing:
                if name not in properties or name not in candidates:
                    continue
                value = candidates[name]
                if self._valid_property(properties[name], value):
                    resolved[name] = value

        remaining = self._missing_required(schema, resolved)
        if not remaining:
            self._validate_complete(skill=skill, schema=schema, arguments=resolved)
        confidence = str(result.get("confidence") or "medium")
        if confidence not in _CONFIDENCE:
            confidence = "medium"
        clarification = str(result.get("clarification") or "").strip()
        if remaining and not clarification:
            clarification = self._fallback_clarification(schema, remaining)
        return InputResolution(
            arguments=resolved,
            missing=remaining,
            clarification=clarification,
            confidence=confidence,
            llm_used=True,
        )

    @staticmethod
    def _missing_required(schema: dict[str, Any], arguments: dict[str, Any]) -> tuple[str, ...]:
        required = schema.get("required", [])
        if not isinstance(required, list):
            return ()
        return tuple(
            str(name)
            for name in required
            if name not in arguments
            or arguments[name] is None
            or (isinstance(arguments[name], str) and not arguments[name].strip())
        )

    @staticmethod
    def _valid_property(schema: Any, value: Any) -> bool:
        if not isinstance(schema, dict):
            return False
        if isinstance(value, str) and not value.strip():
            return False
        return not any(Draft7Validator(schema).iter_errors(value))

    @staticmethod
    def _validate_complete(
        *,
        skill: SkillDefinition,
        schema: dict[str, Any],
        arguments: dict[str, Any],
    ) -> None:
        errors = list(Draft7Validator(schema).iter_errors(arguments))
        if errors:
            details = "; ".join(error.message for error in errors)
            raise SkillInputError(f"input for skill '{skill.name}' is invalid: {details}")

    @staticmethod
    def _fallback_clarification(schema: dict[str, Any], missing: tuple[str, ...]) -> str:
        properties = schema.get("properties", {})
        labels: list[str] = []
        for name in missing:
            details = properties.get(name, {}) if isinstance(properties, dict) else {}
            description = details.get("description") if isinstance(details, dict) else ""
            labels.append(str(description or name))
        return f"我还没识别到任务所需的{'、'.join(labels)}，可以补充一下吗？"


__all__ = ["InputResolution", "SchemaInputResolver"]
