"""Runtime validation of skill input/output against their declared JSON Schema.

Skills declare ``input_schema`` and ``output_schema`` (JSON Schema). Input is
validated as a hard precondition before the handler runs; output is validated
as a soft check that produces warnings without aborting the run. An empty schema
(``{}``) disables validation for that direction.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft7Validator

from .contracts import SkillDefinition


class SkillInputError(Exception):
    """Raised when skill input arguments violate the declared input schema."""


def _errors(schema: dict[str, Any], instance: Any) -> list[str]:
    validator = Draft7Validator(schema)
    messages: list[str] = []
    for error in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
        location = "/".join(str(part) for part in error.path) or "<root>"
        messages.append(f"{location}: {error.message}")
    return messages


def validate_skill_input(skill: SkillDefinition, args: dict[str, Any]) -> None:
    schema = skill.input_schema or {}
    if not schema:
        return
    messages = _errors(schema, args)
    if messages:
        raise SkillInputError(f"input for skill '{skill.name}' is invalid: " + "; ".join(messages))


def validate_skill_output(skill: SkillDefinition, result: Any) -> list[str]:
    schema = skill.output_schema or {}
    if not schema:
        return []
    return _errors(schema, result)
