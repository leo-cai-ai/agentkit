"""把自然语言请求转换为经 Schema 约束的 IntentFrame。"""

from __future__ import annotations

import re
from typing import Any

from .context.models import ContextRenderRequest
from .contracts import IntentFrame, TaskRequest
from .llm_client import LLMRequiredError

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

class IntentDecomposer:
    """使用确定性实体提取辅助一次结构化 LLM 判断。"""

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

    def decompose(
        self,
        request: TaskRequest,
        *,
        agent: Any,
        run_id: str,
    ) -> IntentFrame:
        baseline = self._baseline(request)
        agent_context = request.context.get("agent_context")
        summary = agent_context.get("summary", "") if isinstance(agent_context, dict) else ""
        data = self._context_invoker.invoke_json(
            ContextRenderRequest(
                context_id="runtime.intent",
                tenant_id=self._tenant_id,
                tenant_selector=self._tenant_selector,
                run_id=run_id,
                agent=agent,
                skill=None,
                values={
                    "request.message": request.text,
                    "conversation.summary": str(summary or ""),
                    "request.intent_baseline": {
                        "intent_type": baseline.intent_type,
                        "goal": baseline.goal,
                        "target": baseline.target,
                        "entities": baseline.entities,
                        "confidence": baseline.confidence,
                        "signals": baseline.signals,
                    },
                },
                global_token_limit=min(agent.max_tokens, agent.autonomy_budget.max_tokens),
            )
        ).value
        if not isinstance(data, dict):
            raise LLMRequiredError("Intent LLM 必须返回对象")
        intent_type = str(data.get("intent_type") or baseline.intent_type)
        if intent_type not in ALLOWED_INTENT_TYPES:
            raise LLMRequiredError(f"Intent LLM 返回了无效 intent_type: {intent_type}")
        confidence = str(data.get("confidence") or baseline.confidence)
        if confidence not in ALLOWED_CONFIDENCE:
            confidence = "medium"
        target = sanitize_target(data.get("target"), fallback=baseline.target)
        if intent_type == "platform_question" and target["kind"] == "none":
            target = {"kind": "platform_handler", "name": "default"}
        entities = dict(baseline.entities)
        if isinstance(data.get("entities"), dict):
            entities.update({str(key): value for key, value in data["entities"].items()})
        signals = list(baseline.signals)
        signals.append("llm_required:intent")
        if isinstance(data.get("signals"), list):
            signals.extend(str(item) for item in data["signals"][:8])
        return self._frame(
            request=request,
            language=str(data.get("language") or baseline.language),
            intent_type=intent_type,
            goal=str(data.get("goal") or baseline.goal),
            target=target,
            entities=entities,
            confidence=confidence,
            clarification=str(data.get("clarification") or ""),
            signals=dedupe_signals(signals),
        )

    def _baseline(self, request: TaskRequest) -> IntentFrame:
        text = normalize_text(request.text)
        entities = extract_entities(request)
        platform = detect_platform_handler(text)
        if platform:
            return self._frame(
                request=request,
                language=detect_language(request.text),
                intent_type="platform_question",
                goal=goal_for_platform_target(platform),
                target={"kind": "platform_handler", "name": platform},
                entities=entities,
                confidence="high",
                signals=[f"platform_handler:{platform}"],
            )
        if looks_like_business_task(text=text, entities=entities):
            return self._frame(
                request=request,
                language=detect_language(request.text),
                intent_type="business_task",
                goal=infer_business_goal(text),
                target={"kind": "none", "name": ""},
                entities=entities,
                confidence="medium",
                signals=business_signals(request=request, text=text),
            )
        if text:
            return self._frame(
                request=request,
                language=detect_language(request.text),
                intent_type="chit_chat",
                goal="Respond conversationally",
                target={"kind": "platform_handler", "name": "default"},
                entities=entities,
                confidence="low",
                signals=["no_business_or_platform_goal_detected"],
            )
        return self._frame(
            request=request,
            language=detect_language(request.text),
            intent_type="unknown",
            goal="Clarify the user's goal",
            target={"kind": "none", "name": ""},
            entities=entities,
            confidence="low",
            clarification="Please provide a task or question.",
            signals=["empty_request"],
        )

    @staticmethod
    def _frame(
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
                "needs_external_data": intent_type == "business_task",
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
    return list(dict.fromkeys(signals))


def sanitize_target(value: Any, *, fallback: dict[str, Any]) -> dict[str, str]:
    fallback_kind = str(fallback.get("kind") or "none")
    fallback_name = str(fallback.get("name") or "")
    if fallback_kind not in ALLOWED_TARGET_KINDS:
        fallback_kind, fallback_name = "none", ""
    if not isinstance(value, dict):
        return {"kind": fallback_kind, "name": fallback_name}
    kind = str(value.get("kind") or fallback_kind)
    name = str(value.get("name") or fallback_name)
    if kind not in ALLOWED_TARGET_KINDS:
        return {"kind": fallback_kind, "name": fallback_name}
    if kind == "platform_handler":
        return {"kind": kind, "name": name if name in PLATFORM_HANDLERS else "default"}
    if kind == "business_skill" and name:
        return {"kind": kind, "name": name}
    return {"kind": "none", "name": ""}


def detect_language(text: str) -> str:
    return "zh-CN" if re.search(r"[\u4e00-\u9fff]", text) else "en"


def extract_entities(request: TaskRequest) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    job_id = request.context.get("job_id")
    if not job_id:
        match = re.search(r"\bJOB-\d+\b", request.text, flags=re.IGNORECASE)
        job_id = match.group(0).upper() if match else ""
    if job_id:
        entities["job_id"] = str(job_id)
    candidate_ids = request.context.get("candidate_ids") or re.findall(
        r"\bC-\d+\b", request.text, flags=re.IGNORECASE
    )
    if candidate_ids:
        entities["candidate_ids"] = [str(item).upper() for item in candidate_ids]
    if request.context.get("top_n"):
        entities["top_n"] = request.context["top_n"]
    return entities


def detect_platform_handler(text: str) -> str:
    if is_time_question(text):
        return "time"
    if is_identity_question(text):
        return "identity"
    if is_capability_question(text):
        return "capability"
    return ""


def is_time_question(text: str) -> bool:
    return bool(
        (
            re.search(r"\b(current|now|today|date|time)\b", text)
            and re.search(r"\b(time|date|today|now)\b", text)
        )
        or re.search(r"(现在|当前|此刻|今天).*(时间|几点|几号|日期|星期)|(什么时间)", text)
    )


def is_identity_question(text: str) -> bool:
    return bool(
        re.search(r"\b(who are you|what are you|your name|what'?s your name)\b", text)
        or re.search(r"(你是谁|你叫什么|你叫什么名字)", text)
    )


def is_capability_question(text: str) -> bool:
    return bool(
        re.search(r"\b(what can you do|help|capability|capabilities)\b", text)
        or re.search(r"((你|助手|系统).*(能|可以).*(做|干).*(什么|哪些)|能力|帮助)", text)
    )


def looks_like_business_task(*, text: str, entities: dict[str, Any]) -> bool:
    terms = (
        "rank", "shortlist", "screen", "evaluate", "compare", "recommend",
        "排名", "筛选", "推荐", "评估", "订单", "物流", "退款", "小红书",
    )
    return bool(entities) or any(term in text for term in terms)


def business_signals(*, request: TaskRequest, text: str) -> list[str]:
    signals = []
    if request.context.get("approved_skills"):
        signals.append("approved_skills_context")
    if request.context.get("rejected_skills"):
        signals.append("rejected_skills_context")
    if re.search(r"\bJOB-\d+\b|\bC-\d+\b", text, flags=re.IGNORECASE):
        signals.append("business_entity_in_text")
    signals.append("business_action_or_entities")
    return dedupe_signals(signals)


def infer_business_goal(text: str) -> str:
    if any(term in text for term in ("rank", "shortlist", "排名", "筛选")):
        return "Evaluate and rank candidates for a business request"
    return "Execute a governed business task"


def goal_for_platform_target(target: str) -> str:
    return {
        "time": "Answer the current date or time",
        "identity": "Explain the assistant identity",
        "capability": "Explain available platform capabilities",
    }.get(target, "Respond conversationally")
