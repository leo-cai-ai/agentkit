import pytest

from agentkit.core.contracts import SkillDefinition
from agentkit.core.schema_validation import (
    SkillInputError,
    validate_skill_input,
    validate_skill_output,
)


def _skill(*, input_schema=None, output_schema=None) -> SkillDefinition:
    return SkillDefinition(
        name="candidate.rank",
        domain="hr.recruitment",
        description="",
        input_schema=input_schema or {},
        output_schema=output_schema or {},
        permissions=[],
        execution_mode="plan_execute",
        tools=[],
        handler=lambda ctx, args: {},
    )


INPUT_SCHEMA = {
    "type": "object",
    "required": ["job_id", "candidate_ids"],
    "properties": {
        "job_id": {"type": "string"},
        "candidate_ids": {"type": "array", "items": {"type": "string"}},
        "top_n": {"type": "integer"},
    },
}


def test_valid_input_passes():
    skill = _skill(input_schema=INPUT_SCHEMA)
    validate_skill_input(skill, {"job_id": "JOB-001", "candidate_ids": ["C-1"], "top_n": 3})


def test_missing_required_raises():
    skill = _skill(input_schema=INPUT_SCHEMA)
    with pytest.raises(SkillInputError) as exc:
        validate_skill_input(skill, {"candidate_ids": ["C-1"]})
    assert "job_id" in str(exc.value)


def test_wrong_type_raises():
    skill = _skill(input_schema=INPUT_SCHEMA)
    with pytest.raises(SkillInputError):
        validate_skill_input(skill, {"job_id": "JOB-001", "candidate_ids": "not-a-list"})


def test_empty_input_schema_skips():
    skill = _skill(input_schema={})
    validate_skill_input(skill, {"anything": True})


def test_valid_output_returns_no_warnings():
    schema = {"type": "object", "properties": {"evaluated_count": {"type": "integer"}}}
    skill = _skill(output_schema=schema)
    assert validate_skill_output(skill, {"evaluated_count": 5}) == []


def test_invalid_output_returns_warnings_without_raising():
    schema = {
        "type": "object",
        "required": ["ranked_candidates"],
        "properties": {"ranked_candidates": {"type": "array"}},
    }
    skill = _skill(output_schema=schema)
    warnings = validate_skill_output(skill, {"ranked_candidates": "oops"})
    assert warnings
    assert any("ranked_candidates" in w for w in warnings)


def test_empty_output_schema_skips():
    skill = _skill(output_schema={})
    assert validate_skill_output(skill, {"anything": True}) == []
