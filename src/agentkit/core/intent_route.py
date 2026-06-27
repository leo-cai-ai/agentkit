"""Combined intent + route resolution in a single LLM round trip.

The default pipeline runs two LLM nodes back-to-back: ``understand_intent``
(NLU: classify the message into an IntentFrame) and ``route`` (dispatch: pick a
registered skill). Conceptually these are distinct concerns, but for requests
that must go through the LLM they reason over largely overlapping context, so
this resolver folds them into one call:

    one LLM call -> {intent fields..., "route": {skill_name, reason, confidence}}

Intent and route remain separate *objects* (and route still validates the
suggestion deterministically against the agent's allowed skills), so the
separation of concerns is preserved at the data layer while the round trips drop
from two to one. Opt-in via ``combined_intent_route``; complements the
deterministic fast-path (which handles rule-resolvable requests with zero LLM
calls).
"""

from __future__ import annotations

import json
from typing import Any

from .contracts import IntentFrame, RouteDecision, TaskRequest
from .intent import IntentDecomposer
from .llm_client import require_chat_json
from .prompt_library import PromptLibrary
from .router import IntentRouter

DEFAULT_COMBINED_SYSTEM = (
    "You are the combined intent-and-routing node inside a governed LangGraph "
    "enterprise agent. Return only valid JSON with these keys: intent_type, goal, "
    "target, entities, confidence, signals, and route. "
    "intent_type must be one of: business_task, platform_question, "
    "approval_decision, chit_chat, unknown. "
    "target.kind must be one of: business_skill, platform_handler, none. "
    "route must be an object with keys skill_name, reason, confidence; skill_name "
    "must be one of the provided candidate skill names or null. "
    "confidence values must be high, medium, or low. "
    "First classify the user's intent, then route concrete business actions to the "
    "single best candidate skill. Return route.skill_name null for platform "
    "questions, approvals, chit-chat, unknown requests, or when no candidate skill "
    "is appropriate. Never invent a skill."
)


class CombinedIntentRouter:
    """Resolve intent and route together with one LLM call."""

    def __init__(
        self,
        *,
        intent_decomposer: IntentDecomposer,
        router: IntentRouter,
        tenant_config: dict[str, Any],
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        self._intent = intent_decomposer
        self._router = router
        self._tenant_config = tenant_config
        self._prompts = prompt_library or PromptLibrary()

    def resolve(self, request: TaskRequest) -> tuple[IntentFrame, RouteDecision]:
        deterministic_intent = self._intent.deterministic_intent(request)
        deterministic_route = self._router.deterministic_route(request, intent=deterministic_intent)
        data = require_chat_json(
            self._prompts.system("intent_route", DEFAULT_COMBINED_SYSTEM, persona="router"),
            self._user_prompt(
                request=request,
                deterministic_intent=deterministic_intent,
                deterministic_route=deterministic_route,
            ),
        )
        intent = self._intent.frame_from_llm(request, data)
        route_data = data.get("route")
        if not isinstance(route_data, dict):
            route_data = {}
        route = self._router.decision_from_llm(request, intent=intent, data=route_data)
        return intent, route

    def _user_prompt(
        self,
        *,
        request: TaskRequest,
        deterministic_intent: IntentFrame,
        deterministic_route: RouteDecision,
    ) -> str:
        payload = {
            "message": request.text,
            "request_context": request.context,
            "selected_agent": str(request.context.get("agent") or "").strip(),
            "deterministic_intent": {
                "intent_type": deterministic_intent.intent_type,
                "goal": deterministic_intent.goal,
                "target": deterministic_intent.target,
                "entities": deterministic_intent.entities,
                "confidence": deterministic_intent.confidence,
                "signals": deterministic_intent.signals,
            },
            "deterministic_route": {
                "skill_name": deterministic_route.skill_name,
                "reason": deterministic_route.reason,
                "confidence": deterministic_route.confidence,
            },
            "candidate_skills": self._router.candidate_skills(request),
            "routing_hints": self._tenant_config.get("routing_hints", {}),
            "enabled_domains": self._tenant_config.get("enabled_domains", []),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)
