"""Built-in evaluation targets (LLM prompt and full agent gateway)."""

from __future__ import annotations

import json
from typing import Any

from .case import EvalCase


def llm_target(case: EvalCase) -> str:
    """Send the case's system/user prompt straight to the configured LLM."""
    from agentkit.core import llm_client

    return llm_client.require_chat(case.system, case.user)


def extract_text(response: dict[str, Any]) -> str:
    """Flatten a gateway TaskResponse dict into a single assertable string.

    Prefers a human-facing message, then known business outputs, falling back to
    the JSON-encoded output so structural checks (regex/contains) still work.
    """
    output = response.get("output", {}) or {}
    final = output.get("final", {}) or {}
    if final.get("message"):
        return str(final["message"])
    if output.get("message"):
        return str(output["message"])
    if final.get("summary"):
        return str(final["summary"])
    return json.dumps(output, ensure_ascii=False, default=str)


def make_gateway_target(
    runtime: Any,
    *,
    default_roles: tuple[str, ...] = ("recruiter",),
) -> Any:
    """Build a target that runs each case through the agent gateway end-to-end."""
    from agentkit.core.contracts import TaskRequest

    def target(case: EvalCase) -> str:
        context = dict(case.context)
        if case.agent:
            context.setdefault("agent", case.agent)
        request = TaskRequest(
            user_id="eval",
            roles=list(default_roles),
            text=case.user,
            context=context,
        )
        response = runtime.gateway.handle(request)
        return extract_text(response.to_dict())

    return target


def make_gateway_trace_target(
    runtime: Any,
    *,
    default_roles: tuple[str, ...] = ("recruiter",),
) -> Any:
    """Build a target that returns full response/audit JSON for trajectory checks."""
    from agentkit.core.contracts import TaskRequest

    def target(case: EvalCase) -> str:
        context = dict(case.context)
        resume_decision = context.pop("_eval_resume", None)
        if case.agent:
            context.setdefault("agent", case.agent)
        request = TaskRequest(
            user_id="eval",
            roles=list(default_roles),
            text=case.user,
            context=context,
        )
        initial_response = runtime.gateway.handle(request).to_dict()
        response = initial_response
        initial_output = initial_response.get("output", {})
        if (
            isinstance(resume_decision, dict)
            and initial_output.get("status") == "waiting_for_approval"
        ):
            thread_id = str(initial_output.get("thread_id") or "")
            response = runtime.gateway.resume(
                thread_id,
                approved_skills=list(resume_decision.get("approved_skills", [])),
                rejected_skills=list(resume_decision.get("rejected_skills", [])),
                decision_context={"source": "eval", **resume_decision},
            ).to_dict()
        events = response.get("audit_events", [])
        envelope = {
            "initial_status": initial_output.get("status", "completed"),
            "status": response.get("output", {}).get("status", "completed"),
            "initial_response": initial_response,
            "response": response,
            "audit_event_types": [event.get("type") for event in events if isinstance(event, dict)],
        }
        return json.dumps(envelope, ensure_ascii=False, default=str)

    return target


__all__ = ["llm_target", "make_gateway_target", "make_gateway_trace_target", "extract_text"]
