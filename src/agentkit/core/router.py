"""Business-agnostic intent router."""

from __future__ import annotations

import json
from typing import Any

from .contracts import IntentFrame, RouteDecision, TaskRequest
from .llm_client import LLMRequiredError, require_chat_json
from .prompt_library import PromptLibrary
from .registry import AgentRegistry, SkillRegistry

DEFAULT_ROUTE_SYSTEM = (
    "You are an LLM routing node inside a governed LangGraph enterprise agent. "
    "Return only valid JSON with keys: skill_name, reason, confidence. "
    "skill_name must be one of the candidate skill names or null. "
    "confidence must be high, medium, or low. "
    "Route concrete business actions to the best registered skill. "
    "Return null for platform questions, approvals, chit-chat, unknown requests, "
    "or when no candidate skill is actually appropriate. Never invent a skill."
)


class IntentRouter:
    def __init__(
        self,
        *,
        tenant_config: dict,
        agents: AgentRegistry,
        skills: SkillRegistry,
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        self._tenant_config = tenant_config
        self._agents = agents
        self._skills = skills
        self._prompts = prompt_library or PromptLibrary()

    def route(self, request: TaskRequest, *, intent: IntentFrame) -> RouteDecision:
        enabled_domains, agent_name, allowed_skill_names = self._routing_scope(request)
        candidate_skills = self._candidate_skill_payload(
            enabled_domains=enabled_domains,
            allowed_skill_names=allowed_skill_names,
        )
        deterministic = self._deterministic_route(
            request=request,
            intent=intent,
            enabled_domains=enabled_domains,
            agent_name=agent_name,
            allowed_skill_names=allowed_skill_names,
        )

        data = require_chat_json(
            self._llm_system_prompt(),
            self._llm_user_prompt(
                request=request,
                intent=intent,
                agent_name=agent_name,
                candidate_skills=candidate_skills,
                deterministic=deterministic,
            ),
        )
        return self._decision_from_llm_data(
            data=data,
            deterministic=deterministic,
            candidate_skills=candidate_skills,
            agent_name=agent_name,
        )

    def deterministic_route(self, request: TaskRequest, *, intent: IntentFrame) -> RouteDecision:
        """Rule-based route only (no LLM). Used by the deterministic fast-path."""
        enabled_domains, agent_name, allowed_skill_names = self._routing_scope(request)
        return self._deterministic_route(
            request=request,
            intent=intent,
            enabled_domains=enabled_domains,
            agent_name=agent_name,
            allowed_skill_names=allowed_skill_names,
        )

    def candidate_skills(self, request: TaskRequest) -> list[dict[str, Any]]:
        """Candidate skills visible to this request's agent/domains (no LLM)."""
        enabled_domains, _agent_name, allowed_skill_names = self._routing_scope(request)
        return self._candidate_skill_payload(
            enabled_domains=enabled_domains,
            allowed_skill_names=allowed_skill_names,
        )

    def decision_from_llm(
        self,
        request: TaskRequest,
        *,
        intent: IntentFrame,
        data: dict[str, Any],
    ) -> RouteDecision:
        """Validate an LLM route suggestion deterministically (no LLM call).

        ``data`` is the route portion of a combined intent+route LLM response,
        with keys skill_name/reason/confidence. Used by the combined resolver so
        the route node does not need its own round trip.
        """
        enabled_domains, agent_name, allowed_skill_names = self._routing_scope(request)
        candidate_skills = self._candidate_skill_payload(
            enabled_domains=enabled_domains,
            allowed_skill_names=allowed_skill_names,
        )
        deterministic = self._deterministic_route(
            request=request,
            intent=intent,
            enabled_domains=enabled_domains,
            agent_name=agent_name,
            allowed_skill_names=allowed_skill_names,
        )
        return self._decision_from_llm_data(
            data=data,
            deterministic=deterministic,
            candidate_skills=candidate_skills,
            agent_name=agent_name,
        )

    def _routing_scope(self, request: TaskRequest) -> tuple[set[str], str, set[str] | None]:
        enabled_domains = set(self._tenant_config.get("enabled_domains", []))
        agent_name = str(request.context.get("agent") or "").strip()
        allowed_skill_names = self._allowed_skills_for_agent(agent_name)
        return enabled_domains, agent_name, allowed_skill_names

    def _deterministic_route(
        self,
        *,
        request: TaskRequest,
        intent: IntentFrame,
        enabled_domains: set[str],
        agent_name: str,
        allowed_skill_names: set[str] | None,
    ) -> RouteDecision:
        text = request.text.lower()

        explicit_skill = request.context.get("skill")
        if explicit_skill:
            explicit_skill = str(explicit_skill)
            if allowed_skill_names is not None and explicit_skill not in allowed_skill_names:
                return RouteDecision(
                    skill_name=None,
                    reason=f"skill {explicit_skill} is outside selected agent {agent_name}",
                    confidence="low",
                )
            if not self._skills.has(explicit_skill):
                return RouteDecision(
                    skill_name=None,
                    reason=f"skill {explicit_skill} is not registered",
                    confidence="low",
                )
            return RouteDecision(
                skill_name=explicit_skill,
                reason="request.context.skill explicitly selected a skill",
                confidence="high",
            )

        target = intent.target or {}
        if target.get("kind") == "platform_handler":
            return RouteDecision(
                skill_name=None,
                reason=f"platform handler selected by intent: {target.get('name')}",
                confidence=intent.confidence,
            )

        if intent.intent_type in {"approval_decision", "chit_chat", "unknown"}:
            return RouteDecision(
                skill_name=None,
                reason=f"no business skill selected for intent_type={intent.intent_type}",
                confidence=intent.confidence,
            )

        if target.get("kind") == "business_skill" and target.get("name"):
            skill_name = str(target["name"])
            if self._skills.has(skill_name):
                skill = self._skills.get(skill_name)
                if skill.domain in enabled_domains and self._skill_allowed(
                    skill_name, allowed_skill_names
                ):
                    return RouteDecision(
                        skill_name=skill_name,
                        reason=self._reason("business skill selected by intent target", agent_name),
                        confidence=intent.confidence,
                    )

        best_score = 0
        best_skill_name: str | None = None
        for skill in self._skills.all():
            if skill.domain not in enabled_domains:
                continue
            if not self._skill_allowed(skill.name, allowed_skill_names):
                continue
            score = 0
            for keyword in skill.keywords:
                if keyword.lower() in text:
                    score += 1
            for hint in self._tenant_config.get("routing_hints", {}).get(skill.name, []):
                if str(hint).lower() in text:
                    score += 1
            if score > best_score:
                best_score = score
                best_skill_name = skill.name

        if best_skill_name:
            return RouteDecision(
                skill_name=best_skill_name,
                reason=self._reason(f"matched routing keywords, score={best_score}", agent_name),
                confidence="high" if best_score >= 2 else "medium",
            )

        return RouteDecision(
            skill_name=None,
            reason=self._reason("no enabled skill matched the request", agent_name),
            confidence="low",
        )

    def _llm_system_prompt(self) -> str:
        return self._prompts.system("route", DEFAULT_ROUTE_SYSTEM, persona="router")

    def _llm_user_prompt(
        self,
        *,
        request: TaskRequest,
        intent: IntentFrame,
        agent_name: str,
        candidate_skills: list[dict[str, Any]],
        deterministic: RouteDecision,
    ) -> str:
        payload = {
            "message": request.text,
            "request_context": request.context,
            "selected_agent": agent_name,
            "intent": {
                "intent_type": intent.intent_type,
                "goal": intent.goal,
                "target": intent.target,
                "entities": intent.entities,
                "confidence": intent.confidence,
                "signals": intent.signals,
            },
            "candidate_skills": candidate_skills,
            "routing_hints": self._tenant_config.get("routing_hints", {}),
            "deterministic_suggestion": {
                "skill_name": deterministic.skill_name,
                "reason": deterministic.reason,
                "confidence": deterministic.confidence,
            },
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _candidate_skill_payload(
        self,
        *,
        enabled_domains: set[str],
        allowed_skill_names: set[str] | None,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for skill in self._skills.all():
            if skill.domain not in enabled_domains:
                continue
            if not self._skill_allowed(skill.name, allowed_skill_names):
                continue
            candidates.append(
                {
                    "name": skill.name,
                    "domain": skill.domain,
                    "description": skill.description,
                    "execution_mode": skill.execution_mode,
                    "permissions": skill.permissions,
                    "tools": skill.tools,
                    "keywords": skill.keywords,
                    "tenant_hints": self._tenant_config.get("routing_hints", {}).get(
                        skill.name, []
                    ),
                }
            )
        return candidates

    def _decision_from_llm_data(
        self,
        *,
        data: dict[str, Any],
        deterministic: RouteDecision,
        candidate_skills: list[dict[str, Any]],
        agent_name: str,
    ) -> RouteDecision:
        candidate_names = {str(skill["name"]) for skill in candidate_skills}
        raw_skill = data.get("skill_name")
        skill_name = str(raw_skill).strip() if raw_skill not in {None, ""} else None
        confidence = str(data.get("confidence") or deterministic.confidence)
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        reason = str(data.get("reason") or "LLM route decision")

        if skill_name is None:
            return RouteDecision(
                skill_name=None,
                reason=self._reason(f"LLM selected no business skill: {reason}", agent_name),
                confidence=confidence,  # type: ignore[arg-type]
            )

        if skill_name not in candidate_names:
            raise LLMRequiredError(
                f"Routing LLM selected unavailable skill '{skill_name}'. "
                f"Allowed candidates: {sorted(candidate_names)}"
            )

        return RouteDecision(
            skill_name=skill_name,
            reason=self._reason(f"LLM selected skill: {reason}", agent_name),
            confidence=confidence,  # type: ignore[arg-type]
        )

    def _allowed_skills_for_agent(self, agent_name: str) -> set[str] | None:
        if not agent_name:
            return None
        try:
            agent = self._agents.get(agent_name)
        except KeyError:
            return set()
        return set(agent.allowed_skills)

    def _skill_allowed(self, skill_name: str, allowed_skill_names: set[str] | None) -> bool:
        return allowed_skill_names is None or skill_name in allowed_skill_names

    def _reason(self, reason: str, agent_name: str) -> str:
        return f"{reason}; selected_agent={agent_name}" if agent_name else reason
