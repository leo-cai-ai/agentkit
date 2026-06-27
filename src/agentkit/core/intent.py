"""LLM-required intent understanding for user requests.

This module is the boundary where natural language becomes structured runtime
state. A deterministic pass still extracts cheap hints, but the final
``IntentFrame`` is produced by the configured LLM and validated before the graph
continues.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .contracts import IntentFrame, TaskRequest
from .llm_client import LLMRequiredError, require_chat_json
from .prompt_library import PromptLibrary

ALLOWED_INTENT_TYPES = {
    "business_task",
    "platform_question",
    "approval_decision",
    "chit_chat",
    "unknown",
}
ALLOWED_TARGET_KINDS = {"business_skill", "platform_handler", "none"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
PLATFORM_HANDLERS = {"time", "identity", "capability", "default"}

DEFAULT_INTENT_SYSTEM = (
    "You are an intent decomposition module for a governed enterprise agent runtime. "
    "Return only valid JSON. Do not answer the user. Do not execute tools. "
    "Classify the user message into one IntentFrame-shaped object with keys: "
    "intent_type, goal, target, entities, confidence, clarification, signals. "
    "intent_type must be one of: business_task, platform_question, "
    "approval_decision, chit_chat, unknown. "
    "target.kind must be one of: business_skill, platform_handler, none. "
    "Use platform_handler names only for generic platform requests: "
    "time, identity, capability, default. "
    "Use business_skill only when the request clearly needs "
    "a registered business capability. "
    "If the user asks how a skill works, what a skill does, "
    "or asks to explain a skill such as candidate.rank, "
    "classify it as platform_question with target platform_handler capability; "
    "do not route it as execution. "
    "When deterministic hints identify business entities or tenant routing signals, "
    "preserve those facts. "
    "Return confidence as high, medium, or low."
)


class IntentDecomposer:
    def __init__(
        self,
        *,
        tenant_config: dict[str, Any],
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        self._tenant_config = tenant_config
        self._prompts = prompt_library or PromptLibrary()

    def decompose(self, request: TaskRequest) -> IntentFrame:
        deterministic = self._deterministic_decompose(request)
        return self._decompose_with_llm(
            request=request,
            deterministic=deterministic,
        )

    def deterministic_intent(self, request: TaskRequest) -> IntentFrame:
        """Rule-based intent only (no LLM). Used by the deterministic fast-path."""
        return self._with_signal(self._deterministic_decompose(request), "fastpath:intent")

    def frame_from_llm(self, request: TaskRequest, data: dict[str, Any]) -> IntentFrame:
        """Build an IntentFrame from an already-fetched LLM payload (no LLM call).

        Used by the combined intent+route resolver, which obtains the intent and
        route in a single round trip and reuses this validation/normalization.
        """
        deterministic = self._deterministic_decompose(request)
        return self._frame_from_llm_data(
            request=request,
            deterministic=deterministic,
            data=data,
        )

    def _deterministic_decompose(self, request: TaskRequest) -> IntentFrame:
        text = normalize_text(request.text)
        language = detect_language(request.text)
        entities = extract_entities(request)

        explained_skill = detect_skill_explanation_request(
            text=text, tenant_config=self._tenant_config
        )
        if explained_skill:
            entities["skill_name"] = explained_skill
            return self._frame(
                request=request,
                language=language,
                intent_type="platform_question",
                goal=f"Explain how registered skill {explained_skill} works",
                target={"kind": "platform_handler", "name": "capability"},
                entities=entities,
                confidence="high",
                signals=[f"skill_explanation_request:{explained_skill}"],
            )

        platform_target = detect_platform_handler(text)
        if platform_target:
            return self._frame(
                request=request,
                language=language,
                intent_type="platform_question",
                goal=goal_for_platform_target(platform_target),
                target={"kind": "platform_handler", "name": platform_target},
                entities=entities,
                confidence="high",
                signals=[f"platform_handler:{platform_target}"],
            )

        tenant_hint_signals = tenant_routing_signals(text=text, tenant_config=self._tenant_config)
        if looks_like_business_task(text=text, entities=entities) or tenant_hint_signals:
            return self._frame(
                request=request,
                language=language,
                intent_type="business_task",
                goal=infer_business_goal(text),
                target={"kind": "none", "name": ""},
                entities=entities,
                confidence="medium",
                signals=business_signals(request=request, text=text) + tenant_hint_signals,
            )

        if text:
            return self._frame(
                request=request,
                language=language,
                intent_type="chit_chat",
                goal="Respond conversationally",
                target={"kind": "platform_handler", "name": "default"},
                entities=entities,
                confidence="low",
                signals=["no_business_or_platform_goal_detected"],
            )

        return self._frame(
            request=request,
            language=language,
            intent_type="unknown",
            goal="Clarify the user's goal",
            target={"kind": "none", "name": ""},
            entities=entities,
            confidence="low",
            clarification="Please provide a task or question.",
            signals=["empty_request"],
        )

    def _decompose_with_llm(
        self,
        *,
        request: TaskRequest,
        deterministic: IntentFrame,
    ) -> IntentFrame:
        data = require_chat_json(
            self._llm_system_prompt(),
            self._llm_user_prompt(request, deterministic),
        )

        return self._frame_from_llm_data(
            request=request,
            deterministic=deterministic,
            data=data,
        )

    def _llm_system_prompt(self) -> str:
        return self._prompts.system("intent", DEFAULT_INTENT_SYSTEM)

    def _llm_user_prompt(self, request: TaskRequest, deterministic: IntentFrame) -> str:
        routing_hints = self._tenant_config.get("routing_hints", {})
        known_business_skills = sorted(str(name) for name in routing_hints)
        payload = {
            "message": request.text,
            "request_context": request.context,
            "deterministic_intent": {
                "intent_type": deterministic.intent_type,
                "goal": deterministic.goal,
                "target": deterministic.target,
                "entities": deterministic.entities,
                "confidence": deterministic.confidence,
                "signals": deterministic.signals,
            },
            "registered_business_skills": known_business_skills,
            "routing_hints": routing_hints,
            "enabled_domains": self._tenant_config.get("enabled_domains", []),
        }
        return json.dumps(payload, ensure_ascii=False)

    def _frame_from_llm_data(
        self,
        *,
        request: TaskRequest,
        deterministic: IntentFrame,
        data: dict[str, Any],
    ) -> IntentFrame:
        if not isinstance(data, dict):
            raise LLMRequiredError("Intent LLM returned a non-object payload.")

        explained_skill = detect_skill_explanation_request(
            text=normalize_text(request.text),
            tenant_config=self._tenant_config,
        )
        intent_type = str(data.get("intent_type") or deterministic.intent_type)
        if intent_type not in ALLOWED_INTENT_TYPES:
            raise LLMRequiredError(f"Intent LLM returned invalid intent_type: {intent_type}")

        confidence = str(data.get("confidence") or "medium")
        if confidence not in ALLOWED_CONFIDENCE:
            confidence = "medium"

        target = sanitize_target(
            data.get("target"),
            fallback=deterministic.target,
            known_business_skills=set(self._tenant_config.get("routing_hints", {})),
        )
        if target["kind"] == "none" and intent_type == "platform_question":
            target = {"kind": "platform_handler", "name": "default"}
        if explained_skill:
            intent_type = "platform_question"
            target = {"kind": "platform_handler", "name": "capability"}

        entities = dict(deterministic.entities)
        llm_entities = data.get("entities")
        if isinstance(llm_entities, dict):
            entities.update({str(key): value for key, value in llm_entities.items()})
        if explained_skill:
            entities["skill_name"] = explained_skill

        llm_signals = data.get("signals")
        signals = list(deterministic.signals)
        signals.append("llm_required:intent")
        if isinstance(llm_signals, list):
            signals.extend(str(item) for item in llm_signals[:8])

        return self._frame(
            request=request,
            language=str(data.get("language") or deterministic.language),
            intent_type=intent_type,
            goal=str(data.get("goal") or deterministic.goal),
            target=target,
            entities=entities,
            confidence=confidence,
            clarification=str(data.get("clarification") or ""),
            signals=dedupe_signals(signals),
        )

    def _with_signal(self, frame: IntentFrame, signal: str) -> IntentFrame:
        return IntentFrame(
            raw_text=frame.raw_text,
            language=frame.language,
            intent_type=frame.intent_type,
            goal=frame.goal,
            boundaries=frame.boundaries,
            entities=frame.entities,
            target=frame.target,
            confidence=frame.confidence,
            clarification=frame.clarification,
            signals=dedupe_signals([*frame.signals, signal]),
        )

    def _frame(
        self,
        *,
        request: TaskRequest,
        language: str,
        intent_type: str,
        goal: str,
        target: dict[str, Any],
        entities: dict[str, Any],
        confidence: str,
        signals: list[str],
        clarification: str = "",
    ) -> IntentFrame:
        return IntentFrame(
            raw_text=request.text,
            language=language,
            intent_type=intent_type,  # type: ignore[arg-type]
            goal=goal,
            boundaries={
                "allowed_domains": list(self._tenant_config.get("enabled_domains", [])),
                "needs_external_data": intent_type == "business_task",
                "needs_approval": bool(self._tenant_config.get("approval_required_skills")),
                "risk_level": "medium" if intent_type == "business_task" else "low",
            },
            entities=entities,
            target=target,
            confidence=confidence,  # type: ignore[arg-type]
            clarification=clarification,
            signals=signals,
        )


def normalize_text(text: str) -> str:
    return " ".join(text.lower().strip().split())


def dedupe_signals(signals: list[str]) -> list[str]:
    seen = set()
    unique = []
    for signal in signals:
        if signal in seen:
            continue
        seen.add(signal)
        unique.append(signal)
    return unique


def sanitize_target(
    value: Any,
    *,
    fallback: dict[str, Any],
    known_business_skills: set[str],
) -> dict[str, str]:
    fallback_kind = str(fallback.get("kind") or "none")
    fallback_name = str(fallback.get("name") or "")
    if fallback_kind not in ALLOWED_TARGET_KINDS:
        fallback_kind = "none"
        fallback_name = ""

    if not isinstance(value, dict):
        return {"kind": fallback_kind, "name": fallback_name}

    kind = str(value.get("kind") or fallback_kind)
    name = str(value.get("name") or fallback_name)
    if kind not in ALLOWED_TARGET_KINDS:
        return {"kind": fallback_kind, "name": fallback_name}
    if kind == "platform_handler":
        return {"kind": kind, "name": name if name in PLATFORM_HANDLERS else "default"}
    if kind == "business_skill":
        if name in known_business_skills:
            return {"kind": kind, "name": name}
        return {"kind": "none", "name": ""}
    return {"kind": "none", "name": ""}


def detect_language(text: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh-CN"
    return "en"


def extract_entities(request: TaskRequest) -> dict[str, Any]:
    text = request.text
    entities: dict[str, Any] = {}

    job_id = request.context.get("job_id")
    if not job_id:
        match = re.search(r"\bJOB-\d+\b", text, flags=re.IGNORECASE)
        job_id = match.group(0).upper() if match else ""
    if job_id:
        entities["job_id"] = str(job_id)

    candidate_ids = request.context.get("candidate_ids")
    if not candidate_ids:
        candidate_ids = re.findall(r"\bC-\d+\b", text, flags=re.IGNORECASE)
    if candidate_ids:
        entities["candidate_ids"] = [str(item).upper() for item in candidate_ids]

    top_n = request.context.get("top_n")
    if top_n:
        entities["top_n"] = top_n

    return entities


def tenant_routing_signals(*, text: str, tenant_config: dict[str, Any]) -> list[str]:
    signals: list[str] = []
    routing_hints = tenant_config.get("routing_hints", {})
    if not isinstance(routing_hints, dict):
        return signals

    for skill_name, hints in routing_hints.items():
        if not isinstance(hints, list):
            continue
        for hint in hints:
            normalized_hint = str(hint).lower().strip()
            if normalized_hint and normalized_hint in text:
                signals.append(f"tenant_routing_hint:{skill_name}")
                break
    return signals


def detect_skill_explanation_request(*, text: str, tenant_config: dict[str, Any]) -> str:
    routing_hints = tenant_config.get("routing_hints", {})
    if not isinstance(routing_hints, dict):
        return ""

    explanation_terms = (
        "怎么工作",
        "如何工作",
        "怎么运行",
        "如何运行",
        "工作原理",
        "原理",
        "介绍",
        "解释",
        "说明",
        "是什么",
        "what is",
        "how does",
        "how do",
        "how it works",
        "work",
        "works",
        "explain",
        "describe",
        "tell me about",
    )
    if not any(term in text for term in explanation_terms):
        return ""

    for skill_name in routing_hints:
        normalized_name = str(skill_name).lower()
        if normalized_name and normalized_name in text:
            return str(skill_name)
    return ""


def detect_platform_handler(text: str) -> str:
    if is_time_question(text):
        return "time"
    if is_identity_question(text):
        return "identity"
    if is_capability_question(text):
        return "capability"
    return ""


def is_time_question(text: str) -> bool:
    if re.search(r"\b(current|now|today|date|time)\b", text) and re.search(
        r"\b(time|date|today|now)\b", text
    ):
        return True
    return bool(
        re.search(
            r"(\u73b0\u5728|\u5f53\u524d|\u6b64\u523b|\u4eca\u5929).*(\u65f6\u95f4|\u51e0\u70b9|\u51e0\u53f7|\u65e5\u671f|\u661f\u671f)|(\u4ec0\u4e48\u65f6\u95f4)",
            text,
        )
    )


def is_identity_question(text: str) -> bool:
    if re.search(r"\b(who are you|what are you|your name|what'?s your name)\b", text):
        return True
    return bool(
        re.search(
            r"(\u4f60\u662f\u8c01|\u4f60\u53eb\u4ec0\u4e48|\u4f60\u53eb\u4ec0\u4e48\u540d\u5b57)",
            text,
        )
    )


def is_capability_question(text: str) -> bool:
    if re.search(r"\b(what can you do|help|capability|capabilities)\b", text):
        return True
    return bool(
        re.search(
            r"((\u4f60|\u52a9\u624b|\u7cfb\u7edf).*(\u80fd|\u53ef\u4ee5).*(\u505a|\u5e72).*(\u4ec0\u4e48|\u54ea\u4e9b)|\u80fd\u529b|\u5e2e\u52a9)",
            text,
        )
    )


def looks_like_business_task(*, text: str, entities: dict[str, Any]) -> bool:
    action_terms = (
        "rank",
        "shortlist",
        "screen",
        "evaluate",
        "compare",
        "recommend",
        "\u6392\u540d",
        "\u7b5b\u9009",
        "\u63a8\u8350",
        "\u8bc4\u4f30",
    )
    has_action = any(term in text for term in action_terms)
    has_entity_in_text = bool(
        re.search(r"\bJOB-\d+\b|\bC-\d+\b", text, flags=re.IGNORECASE)
        or "candidate" in text
        or "\u5019\u9009\u4eba" in text
    )
    return has_action or has_entity_in_text


def business_signals(*, request: TaskRequest, text: str) -> list[str]:
    signals = []
    if request.context.get("approved_skills"):
        signals.append("approved_skills_context")
    if request.context.get("rejected_skills"):
        signals.append("rejected_skills_context")
    if any(
        term in text for term in ("rank", "shortlist", "screen", "\u6392\u540d", "\u7b5b\u9009")
    ):
        signals.append("business_action")
    if re.search(r"\bJOB-\d+\b|\bC-\d+\b", text, flags=re.IGNORECASE):
        signals.append("business_entity_in_text")
    if not signals:
        signals.append("business_action_or_entities")
    return signals


def infer_business_goal(text: str) -> str:
    if any(term in text for term in ("rank", "shortlist", "\u6392\u540d", "\u7b5b\u9009")):
        return "Evaluate and rank candidates for a business request"
    return "Execute a governed business task"


def goal_for_platform_target(target: str) -> str:
    if target == "time":
        return "Answer the current date or time"
    if target == "identity":
        return "Explain the assistant identity"
    if target == "capability":
        return "Explain available platform capabilities"
    return "Respond conversationally"
