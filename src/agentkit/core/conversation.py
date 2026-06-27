"""Runtime-level conversational fallback for non-business requests."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import IntentFrame, TaskRequest
from .prompt_library import PromptLibrary


class ConversationFallback:
    """Answer lightweight platform questions without registering a skill.

    Intent classification happens before routing. This class only renders the
    platform response selected by the structured IntentFrame.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        tenant_config: dict,
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._tenant_config = tenant_config
        self._prompts = prompt_library or PromptLibrary()

    def respond(
        self,
        request: TaskRequest,
        *,
        intent: IntentFrame,
        route_reason: str,
    ) -> dict:
        text = request.text.strip()
        intent_name = str((intent.target or {}).get("name") or "default")
        message = self._message_for(
            intent_name=intent_name,
            intent=intent,
            request=request,
            route_reason=route_reason,
        )

        return {
            "steps": [],
            "final": {
                "message": message,
                "conversation": True,
                "conversation_intent": intent_name,
                "intent_type": intent.intent_type,
                "goal": intent.goal,
                "question": text,
                "route_reason": route_reason,
                "agent_name": self._agent_name(),
                "prompt_used": "agents.general"
                if self._tenant_config.get("prompts", {}).get("agents.general")
                else "",
            },
        }

    def _message_for(
        self,
        *,
        intent_name: str,
        intent: IntentFrame,
        request: TaskRequest,
        route_reason: str,
    ) -> str:
        llm_intents = {"time", "identity", "capability", "default"}
        llm_intent_types = {"chit_chat", "business_task", "unknown"}
        if intent_name in llm_intents or intent.intent_type in llm_intent_types:
            return self._llm_reply(
                request=request,
                intent=intent,
                intent_name=intent_name,
                route_reason=route_reason,
            )
        return self._default_message()

    def _llm_reply(
        self,
        *,
        request: TaskRequest,
        intent: IntentFrame,
        intent_name: str,
        route_reason: str,
    ) -> str:
        from .llm_client import require_chat_streaming

        skills = self._enabled_skill_names() or "none configured"
        skill_context = self._skill_context(intent)
        demo_prompt = self._tenant_config.get("ui", {}).get(
            "demo_prompt",
            "Rank the top 3 candidates for JOB-001 and explain why.",
        )
        now = self._now()
        system = (
            f"You are {self._agent_name()}, {self._agent_description()}, serving tenant "
            f"{self._tenant_id}. You route governed business requests to registered skills "
            f"and answer platform questions. Enabled business skills: {skills}. "
            f"Relevant skill catalog entry: {skill_context}. "
            f"An example task you can run: {demo_prompt}. "
            f"Current grounded time: {now.strftime('%Y-%m-%d %H:%M:%S %z')}. "
            f"Intent target: {intent_name}. Route reason: {route_reason}. "
            "Answer the user briefly (<=80 words), in their language. Only describe "
            "capabilities listed above; never invent skills, data, or results. If the "
            "request needs a capability you do not have, say so and suggest a concrete "
            "governed task instead."
        )
        system = self._prompts.system("conversation", system, persona="general")
        return require_chat_streaming(system, request.text.strip())

    def _default_message(self) -> str:
        return (
            f"I am {self._agent_name()}. I did not detect a registered business "
            "task in your message, so I handled it as a normal conversation. You "
            "can ask a platform question or submit a concrete business request."
        )

    def _agent_name(self) -> str:
        return self._agent_identity().get("name", "Enterprise AI Workforce")

    def _agent_description(self) -> str:
        return self._agent_identity().get(
            "description",
            "a generic enterprise agent runtime for governed business execution",
        )

    def _agent_identity(self) -> dict[str, Any]:
        identity = self._tenant_config.get("agent_identity", {})
        return identity if isinstance(identity, dict) else {}

    def _enabled_skill_names(self) -> str:
        hints = self._tenant_config.get("routing_hints", {})
        return ", ".join(sorted(str(name) for name in hints))

    def _skill_context(self, intent: IntentFrame) -> dict[str, Any] | str:
        requested_skill = str((intent.entities or {}).get("skill_name") or "")
        catalog = self._tenant_config.get("skill_catalog", [])
        if isinstance(catalog, list):
            for item in catalog:
                if isinstance(item, dict) and item.get("name") == requested_skill:
                    return item
        return "none"

    def _now(self) -> datetime:
        timezone_name = self._tenant_config.get("timezone")
        if timezone_name:
            try:
                return datetime.now(ZoneInfo(str(timezone_name)))
            except Exception:
                pass
        return datetime.now().astimezone()
