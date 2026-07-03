"""Context Pack 声明、渲染请求和调用结果契约。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentkit.core.contracts import AgentProfile, SkillDefinition


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ContextTemplatesModel(StrictModel):
    system: str = Field(min_length=1)
    user: str = Field(min_length=1)


class ContextInstructionsModel(StrictModel):
    agent: bool = False
    skill: bool = False


class ContextInputModel(StrictModel):
    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    source: str = Field(min_length=1)
    required: bool = False
    priority: int = Field(default=50, ge=0, le=100)
    serializer: str = "text"
    max_items: int | None = Field(default=None, gt=0)
    max_chars: int | None = Field(default=None, gt=0)
    truncate: Literal["head", "tail", "newest", "highest_score"] = "tail"


class ContextLimitsModel(StrictModel):
    max_input_tokens: int = Field(gt=0)
    response_reserve_tokens: int = Field(ge=0)


class ContextOutputModel(StrictModel):
    mode: Literal["text", "json"] = "text"
    schema_path: str | None = Field(default=None, alias="schema")

    @model_validator(mode="after")
    def validate_schema_requirement(self) -> ContextOutputModel:
        if self.mode == "json" and not self.schema_path:
            raise ValueError("JSON 输出必须声明 schema")
        if self.mode == "text" and self.schema_path:
            raise ValueError("Text 输出不能声明 schema")
        return self


class ContextAuditModel(StrictModel):
    record_input_names: bool = True
    record_content_hashes: bool = True
    record_rendered_content: bool = False


class ContextDefinitionModel(StrictModel):
    id: str = Field(pattern=r"^(runtime|skill)\.[a-z0-9][a-z0-9.-]*$")
    version: int = Field(gt=0)
    owner: Literal["runtime", "skill"]
    templates: ContextTemplatesModel
    fragments: list[str] = Field(default_factory=list)
    instructions: ContextInstructionsModel = Field(default_factory=ContextInstructionsModel)
    inputs: list[ContextInputModel] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    limits: ContextLimitsModel
    output: ContextOutputModel = Field(default_factory=ContextOutputModel)
    audit: ContextAuditModel = Field(default_factory=ContextAuditModel)


@dataclass(frozen=True)
class ContextDefinition:
    model: ContextDefinitionModel
    source_dir: Path
    system_template: str
    user_template: str
    fragments: tuple[str, ...]
    output_schema: dict[str, Any] | None
    content_hash: str
    override_hash: str = ""


@dataclass(frozen=True)
class ContextRenderRequest:
    context_id: str
    tenant_id: str
    tenant_selector: str
    run_id: str
    agent: AgentProfile | None
    skill: SkillDefinition | None
    values: Mapping[str, Any]
    global_token_limit: int


@dataclass(frozen=True)
class RenderedContext:
    context_id: str
    version: int
    system: str
    user: str
    output_schema: dict[str, Any] | None
    content_hash: str
    override_hash: str
    estimated_input_tokens: int
    included_inputs: tuple[str, ...]
    truncated_inputs: tuple[str, ...]
    truncation_details: tuple[dict[str, int | str], ...] = ()


@dataclass(frozen=True)
class LLMInvocationResult:
    value: Any
    rendered: RenderedContext
    estimated_output_tokens: int


__all__ = [
    "ContextAuditModel",
    "ContextDefinition",
    "ContextDefinitionModel",
    "ContextInputModel",
    "ContextInstructionsModel",
    "ContextLimitsModel",
    "ContextOutputModel",
    "ContextRenderRequest",
    "ContextTemplatesModel",
    "LLMInvocationResult",
    "RenderedContext",
]
