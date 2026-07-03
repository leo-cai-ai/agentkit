"""把结构化意图解析为 Agent 白名单内的 CapabilityResolution。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

from .contracts import AgentProfile, IntentFrame, SkillDefinition, TaskRequest
from .execution.models import CapabilityResolution, ComplexityAssessment, ToolPolicy
from .llm_client import require_chat_json
from .prompt_library import PromptLibrary
from .registry import AgentRegistry, SkillRegistry

DEFAULT_ROUTE_SYSTEM = (
    "你是企业 Agent 的能力解析节点。只返回 JSON，字段为 primary_skill、"
    "candidate_skills、reason、confidence、has_dependencies。只能从候选能力中选择，"
    "不能回答用户，也不能创造 Skill。"
)


class CapabilityResolutionError(ValueError):
    """请求尝试越过 Agent 的 Capability 边界。"""


Suggestion = Callable[[str, str], dict[str, Any]]


class IntentRouter:
    """确定性规则优先，低置信度时才接受受约束的 LLM 建议。"""

    def __init__(
        self,
        *,
        agents: AgentRegistry,
        skills: SkillRegistry,
        prompt_library: PromptLibrary | None = None,
        suggestion: Suggestion | None = None,
    ) -> None:
        self._agents = agents
        self._skills = skills
        self._prompts = prompt_library or PromptLibrary()
        self._suggestion = suggestion or require_chat_json

    def resolve(
        self,
        request: TaskRequest,
        *,
        intent: IntentFrame,
    ) -> CapabilityResolution:
        agent = self._request_agent(request)
        if intent.intent_type in {"platform_question", "approval_decision", "chit_chat", "unknown"}:
            return self._answer_resolution(intent)
        target = intent.target or {}
        if target.get("kind") == "platform_handler":
            return self._answer_resolution(intent)

        explicit_many = request.context.get("skills")
        if isinstance(explicit_many, list) and explicit_many:
            candidates = self._validate_candidates(agent, explicit_many)
            return self._resolution(
                request=request,
                intent=intent,
                candidates=candidates,
                primary=None,
                reason="请求显式选择多个 Capability",
                confidence="high",
            )

        explicit = request.context.get("skill")
        if explicit:
            skill_name = str(explicit)
            self._validate_candidates(agent, [skill_name])
            return self._resolution(
                request=request,
                intent=intent,
                candidates=(skill_name,),
                primary=skill_name,
                reason="请求显式选择 Capability",
                confidence="high",
            )

        if target.get("kind") == "business_skill" and target.get("name"):
            skill_name = str(target["name"])
            self._validate_candidates(agent, [skill_name])
            return self._resolution(
                request=request,
                intent=intent,
                candidates=(skill_name,),
                primary=skill_name,
                reason="结构化意图指定 Capability",
                confidence=intent.confidence,
            )

        scored = self._score_skills(agent, request.text)
        if scored and scored[0][0] >= 1:
            best_score = scored[0][0]
            tied = tuple(name for score, name in scored if score == best_score)
            if len(tied) == 1 or best_score >= 2:
                primary = tied[0]
                return self._resolution(
                    request=request,
                    intent=intent,
                    candidates=(primary,),
                    primary=primary,
                    reason=f"命中 Capability 关键词，score={best_score}",
                    confidence="high" if best_score >= 2 else "medium",
                )

        return self._resolve_with_suggestion(request=request, intent=intent, agent=agent)

    def candidate_skills(self, request: TaskRequest) -> list[dict[str, Any]]:
        agent = self._request_agent(request)
        return [self._skill_payload(self._skills.get(name)) for name in agent.allowed_skills]

    def _resolve_with_suggestion(
        self,
        *,
        request: TaskRequest,
        intent: IntentFrame,
        agent: AgentProfile,
    ) -> CapabilityResolution:
        candidates = [self._skill_payload(self._skills.get(name)) for name in agent.allowed_skills]
        data = self._suggestion(
            self._llm_system_prompt(),
            json.dumps(
                {
                    "message": request.text,
                    "intent": {
                        "goal": intent.goal,
                        "entities": intent.entities,
                        "confidence": intent.confidence,
                    },
                    "candidate_skills": candidates,
                },
                ensure_ascii=False,
            ),
        )
        raw_candidates = data.get("candidate_skills") or []
        if not isinstance(raw_candidates, list):
            raise CapabilityResolutionError("LLM candidate_skills 必须是列表")
        selected = self._validate_candidates(agent, raw_candidates)
        raw_primary = data.get("primary_skill")
        primary = str(raw_primary) if raw_primary else None
        if primary and primary not in selected:
            raise CapabilityResolutionError("LLM primary_skill 不在候选集合中")
        if not selected:
            return self._answer_resolution(intent)
        return self._resolution(
            request=request,
            intent=intent,
            candidates=selected,
            primary=primary if len(selected) == 1 else None,
            reason=str(data.get("reason") or "受约束的 LLM Capability 建议"),
            confidence=_confidence(data.get("confidence")),
            llm_dependencies=bool(data.get("has_dependencies", False)),
        )

    def _resolution(
        self,
        *,
        request: TaskRequest,
        intent: IntentFrame,
        candidates: tuple[str, ...],
        primary: str | None,
        reason: str,
        confidence: str,
        llm_dependencies: bool = False,
    ) -> CapabilityResolution:
        selected = [self._skills.get(name) for name in candidates]
        context = request.context
        batch_items = 0
        if primary:
            skill = self._skills.get(primary)
            if skill.batch_key:
                raw = context.get(skill.batch_key)
                if raw is None and isinstance(context.get("skill_args"), dict):
                    raw = context["skill_args"].get(skill.batch_key)
                if isinstance(raw, list):
                    batch_items = len(raw)
        has_dependencies = bool(context.get("has_dependencies", llm_dependencies))
        complexity = ComplexityAssessment(
            candidate_skills=candidates,
            estimated_steps=max(1, int(context.get("estimated_steps", len(candidates) or 1))),
            has_dependencies=has_dependencies,
            needs_dynamic_observation=any(
                skill.execution.reasoning.value == "react" for skill in selected
            ),
            has_side_effects=any(
                skill.execution.tool_policy is ToolPolicy.SIDE_EFFECT for skill in selected
            ),
            batch_items=batch_items,
            independent_skills=(
                len(candidates) if len(candidates) > 1 and not has_dependencies else 0
            ),
            missing_information=bool(context.get("missing_information", False)),
            confidence=_confidence(confidence),
        )
        return CapabilityResolution(
            response_mode="multi_skill" if len(candidates) > 1 else "skill",
            primary_skill=primary,
            candidate_skills=candidates,
            reason=reason,
            confidence=_confidence(confidence),
            complexity=complexity,
        )

    def _request_agent(self, request: TaskRequest) -> AgentProfile:
        agent_id = str(request.context.get("agent") or "").strip()
        if not agent_id:
            raise CapabilityResolutionError("请求必须显式指定 agent")
        try:
            return self._agents.get(agent_id)
        except KeyError as exc:
            raise CapabilityResolutionError(f"未知 Agent: {agent_id}") from exc

    def _validate_candidates(
        self,
        agent: AgentProfile,
        candidates: list[Any],
    ) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(item) for item in candidates if str(item)))
        unavailable = [name for name in normalized if name not in agent.allowed_skills]
        if unavailable:
            raise CapabilityResolutionError(
                f"Capability 未绑定到 Agent {agent.name}: {', '.join(unavailable)}"
            )
        missing = [name for name in normalized if not self._skills.has(name)]
        if missing:
            raise CapabilityResolutionError(f"Capability 未注册: {', '.join(missing)}")
        return normalized

    def _score_skills(self, agent: AgentProfile, text: str) -> list[tuple[int, str]]:
        normalized = text.casefold()
        scored = []
        for name in agent.allowed_skills:
            skill = self._skills.get(name)
            score = sum(1 for keyword in skill.keywords if keyword.casefold() in normalized)
            scored.append((score, name))
        return sorted(scored, key=lambda item: (-item[0], item[1]))

    def _skill_payload(self, skill: SkillDefinition) -> dict[str, Any]:
        return {
            "name": skill.name,
            "description": skill.description,
            "reasoning": skill.execution.reasoning.value,
            "orchestration": skill.execution.orchestration.value,
            "tool_policy": skill.execution.tool_policy.value,
            "keywords": skill.keywords,
        }

    def _answer_resolution(self, intent: IntentFrame) -> CapabilityResolution:
        return CapabilityResolution(
            response_mode="answer",
            primary_skill=None,
            candidate_skills=(),
            reason=f"无需业务 Capability: {intent.intent_type}",
            confidence=intent.confidence,
            complexity=ComplexityAssessment(confidence=intent.confidence),
        )

    def _llm_system_prompt(self) -> str:
        return self._prompts.system("route", DEFAULT_ROUTE_SYSTEM, persona="router")


def _confidence(value: Any) -> Literal["high", "medium", "low"]:
    normalized = str(value or "medium")
    if normalized == "high":
        return "high"
    if normalized == "low":
        return "low"
    return "medium"


__all__ = ["CapabilityResolutionError", "IntentRouter"]
