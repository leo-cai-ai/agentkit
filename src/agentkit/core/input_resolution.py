"""Resolve natural-language requests into validated skill input arguments."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from jsonschema import Draft7Validator

from .contracts import IntentFrame, RouteDecision, TaskRequest
from .llm_client import LLMRequiredError, require_chat_json
from .prompt_library import PromptLibrary
from .registry import SkillRegistry


@dataclass(frozen=True)
class SlotEvidence:
    """Traceable evidence for one resolved skill input."""

    value: Any
    source: str
    confidence: float
    default_reason: str = ""


@dataclass(frozen=True)
class InputResolution:
    """Result of resolving a request against one skill's JSON Schema."""

    skill_name: str | None
    arguments: dict[str, Any] = field(default_factory=dict)
    slots: dict[str, SlotEvidence] = field(default_factory=dict)
    missing_required: list[str] = field(default_factory=list)
    invalid_fields: list[str] = field(default_factory=list)
    clarification: str = ""
    llm_used: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing_required and not self.invalid_fields

    def to_audit(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "arguments": dict(self.arguments),
            "slots": {name: asdict(evidence) for name, evidence in self.slots.items()},
            "missing_required": list(self.missing_required),
            "invalid_fields": list(self.invalid_fields),
            "clarification": self.clarification,
            "complete": self.complete,
            "llm_used": self.llm_used,
            "errors": list(self.errors),
        }


class SkillInputResolver:
    """Hybrid resolver: trusted structure first, semantic LLM extraction second."""

    def __init__(
        self,
        *,
        tenant_config: dict[str, Any],
        skills: SkillRegistry,
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        self._tenant_config = tenant_config
        self._skills = skills
        self._prompts = prompt_library or PromptLibrary()

    def resolve(
        self,
        *,
        request: TaskRequest,
        intent: IntentFrame,
        route: RouteDecision,
    ) -> InputResolution:
        if route.skill_name is None:
            return InputResolution(skill_name=None)

        skill = self._skills.get(route.skill_name)
        schema = skill.input_schema or {}
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            return InputResolution(
                skill_name=skill.name,
                arguments=dict(request.context),
            )

        arguments: dict[str, Any] = {}
        slots: dict[str, SlotEvidence] = {}
        errors: list[str] = []
        invalid_fields: list[str] = []
        self._merge_known_inputs(
            request=request,
            intent=intent,
            properties=properties,
            arguments=arguments,
            slots=slots,
            errors=errors,
            invalid_fields=invalid_fields,
        )

        required = _required_fields(schema)
        unresolved = [name for name in properties if name not in arguments]
        missing_required = [name for name in required if name not in arguments]
        infer_from_message = bool(schema.get("x-agentkit-infer-from-message"))
        llm_used = False
        llm_clarification = ""

        if missing_required or (infer_from_message and unresolved):
            llm_used = True
            try:
                data = require_chat_json(
                    self._prompts.system(
                        "input_resolution",
                        _resolution_system_prompt(),
                    ),
                    _resolution_user_prompt(
                        request=request,
                        intent=intent,
                        skill_name=skill.name,
                        skill_description=skill.description,
                        schema=schema,
                        known_arguments=arguments,
                    ),
                )
                llm_clarification = str(data.get("clarification") or "").strip()
                self._merge_llm_inputs(
                    data=data,
                    properties=properties,
                    required=required,
                    arguments=arguments,
                    slots=slots,
                    errors=errors,
                    invalid_fields=invalid_fields,
                )
            except LLMRequiredError as exc:
                errors.append(f"semantic extraction unavailable: {exc}")

        self._apply_schema_defaults(
            properties=properties,
            arguments=arguments,
            slots=slots,
            errors=errors,
            required=required,
            invalid_fields=invalid_fields,
        )
        missing_required = [
            name for name in required if name not in arguments or _is_empty(arguments[name])
        ]
        clarification = ""
        if missing_required or invalid_fields:
            clarification = (
                llm_clarification
                if missing_required and not invalid_fields and llm_clarification
                else _default_clarification(
                    language=intent.language,
                    skill_name=skill.name,
                    missing=missing_required,
                    invalid=invalid_fields,
                    properties=properties,
                )
            )

        return InputResolution(
            skill_name=skill.name,
            arguments=arguments,
            slots=slots,
            missing_required=missing_required,
            invalid_fields=invalid_fields,
            clarification=clarification,
            llm_used=llm_used,
            errors=errors,
        )

    def _merge_known_inputs(
        self,
        *,
        request: TaskRequest,
        intent: IntentFrame,
        properties: dict[str, Any],
        arguments: dict[str, Any],
        slots: dict[str, SlotEvidence],
        errors: list[str],
        invalid_fields: list[str],
    ) -> None:
        skill_args = request.context.get("skill_args")
        sources: list[tuple[str, dict[str, Any], float]] = []
        if isinstance(skill_args, dict):
            sources.append(("request_skill_args", skill_args, 1.0))
        sources.append(("request_context", request.context, 1.0))
        intent_source = "intent_llm" if "llm_required:intent" in intent.signals else "intent_rule"
        sources.append((intent_source, intent.entities, 0.9))

        for source, values, confidence in sources:
            for name, property_schema in properties.items():
                if name in arguments or name not in values or _is_empty(values[name]):
                    continue
                value = _coerce_value(values[name], property_schema)
                if _valid_property(property_schema, value):
                    arguments[name] = value
                    slots[name] = SlotEvidence(
                        value=value,
                        source=source,
                        confidence=confidence,
                    )
                else:
                    errors.append(f"ignored invalid {source} value for '{name}'")
                    if (
                        source in {"request_skill_args", "request_context"}
                        and name not in invalid_fields
                    ):
                        invalid_fields.append(name)

    def _merge_llm_inputs(
        self,
        *,
        data: dict[str, Any],
        properties: dict[str, Any],
        required: list[str],
        arguments: dict[str, Any],
        slots: dict[str, SlotEvidence],
        errors: list[str],
        invalid_fields: list[str],
    ) -> None:
        candidates = data.get("arguments")
        if not isinstance(candidates, dict):
            errors.append("semantic extraction returned no arguments object")
            return
        confidence_map = data.get("confidence")
        if not isinstance(confidence_map, dict):
            confidence_map = {}
        min_confidence = _minimum_confidence(self._tenant_config)

        for name, raw_value in candidates.items():
            if name in arguments or name not in properties or _is_empty(raw_value):
                continue
            property_schema = properties[name]
            value = _coerce_value(raw_value, property_schema)
            confidence = _confidence_value(confidence_map.get(name))
            if not _valid_property(property_schema, value):
                errors.append(f"ignored invalid semantic value for '{name}'")
                if name not in invalid_fields:
                    invalid_fields.append(name)
                continue
            threshold = min_confidence if name in required else min(0.5, min_confidence)
            if confidence < threshold:
                errors.append(
                    f"ignored low-confidence semantic value for '{name}' ({confidence:.2f})"
                )
                continue
            arguments[name] = value
            slots[name] = SlotEvidence(
                value=value,
                source="llm_slot_extraction",
                confidence=confidence,
            )

    def _apply_schema_defaults(
        self,
        *,
        properties: dict[str, Any],
        arguments: dict[str, Any],
        slots: dict[str, SlotEvidence],
        errors: list[str],
        required: list[str],
        invalid_fields: list[str],
    ) -> None:
        for name, property_schema in properties.items():
            if (
                name in arguments
                or name in required
                or name in invalid_fields
                or not isinstance(property_schema, dict)
            ):
                continue
            if "default" not in property_schema:
                continue
            value = _coerce_value(property_schema["default"], property_schema)
            if not _valid_property(property_schema, value):
                errors.append(f"ignored invalid schema default for '{name}'")
                continue
            arguments[name] = value
            slots[name] = SlotEvidence(
                value=value,
                source="schema_default",
                confidence=1.0,
                default_reason="optional input was not supplied or inferred",
            )


def _resolution_system_prompt() -> str:
    return (
        "You extract input arguments for exactly one already-selected enterprise skill. "
        "Return only valid JSON with keys: arguments, confidence, missing_required, "
        "clarification. arguments may contain only fields declared in input_schema. "
        "confidence must map every returned field to a number from 0 to 1. "
        "Use the user's meaning, not a fixed sentence format. Preserve proper nouns and "
        "the user's language. Do not invent absent business facts, identifiers, metrics, "
        "or approvals. Do not apply schema defaults; the runtime applies them later. "
        "If a required value cannot be inferred reliably, omit it, list it in "
        "missing_required, and ask one concise clarification question."
    )


def _resolution_user_prompt(
    *,
    request: TaskRequest,
    intent: IntentFrame,
    skill_name: str,
    skill_description: str,
    schema: dict[str, Any],
    known_arguments: dict[str, Any],
) -> str:
    payload = {
        "message": request.text,
        "language": intent.language,
        "intent_goal": intent.goal,
        "intent_entities": intent.entities,
        "selected_skill": {
            "name": skill_name,
            "description": skill_description,
            "input_schema": schema,
        },
        "known_arguments": known_arguments,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _required_fields(schema: dict[str, Any]) -> list[str]:
    value = schema.get("required")
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _minimum_confidence(tenant_config: dict[str, Any]) -> float:
    config = tenant_config.get("input_resolution")
    raw = config.get("min_confidence", 0.7) if isinstance(config, dict) else 0.7
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.7


def _confidence_value(value: Any) -> float:
    if isinstance(value, str):
        labels = {"high": 0.9, "medium": 0.7, "low": 0.3}
        if value.lower() in labels:
            return labels[value.lower()]
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _coerce_value(value: Any, schema: Any) -> Any:
    if not isinstance(schema, dict):
        return value
    expected = schema.get("type")
    if expected == "integer" and isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    if expected == "number" and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    if expected == "boolean" and isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return value


def _valid_property(schema: Any, value: Any) -> bool:
    if not isinstance(schema, dict):
        return True
    return Draft7Validator(schema).is_valid(value)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | tuple | dict | set):
        return len(value) == 0
    return False


def _default_clarification(
    *,
    language: str,
    skill_name: str,
    missing: list[str],
    invalid: list[str],
    properties: dict[str, Any],
) -> str:
    def labels_for(names: list[str]) -> str:
        labels = []
        for name in names:
            schema = properties.get(name)
            label = schema.get("x-agentkit-label") if isinstance(schema, dict) else None
            labels.append(f"{label or name} ({name})")
        separator = "、" if language == "zh-CN" else ", "
        return separator.join(labels)

    missing_labels = labels_for(missing)
    invalid_labels = labels_for(invalid)
    if language == "zh-CN":
        parts = []
        if missing_labels:
            parts.append(f"请补充：{missing_labels}")
        if invalid_labels:
            parts.append(f"请修正超出格式或范围的字段：{invalid_labels}")
        return f"为了继续执行 {skill_name}，{'；'.join(parts)}。直接用自然语言回答即可。"

    parts = []
    if missing_labels:
        parts.append(f"provide: {missing_labels}")
    if invalid_labels:
        parts.append(f"correct invalid or out-of-range fields: {invalid_labels}")
    return (
        f"To continue with {skill_name}, please {'; '.join(parts)}. "
        "You can answer in natural language."
    )
