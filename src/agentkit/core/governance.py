"""Governance extension points for plan review, approval, and output review."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from .approvals import evaluate_approval
from .contracts import TaskPlan, TaskRequest
from .llm_client import LLMRequiredError, require_chat_json
from .prompt_library import PromptLibrary

DEFAULT_PLAN_REVIEW_SYSTEM = (
    "You are the LLM plan-review node for a governed enterprise agent. "
    "Return only valid JSON with keys: status, reason, findings. "
    "status must be approved, approved_with_warnings, rejected, or skipped. "
    "Findings must be a list of objects with severity and message. "
    "Check whether the plan matches the user's request, uses only the routed skill, "
    "has required args, respects tenant governance, and exposes risks before execution."
)

DEFAULT_APPROVAL_SYSTEM = (
    "You are the LLM approval-governance node. Return only valid JSON with keys: "
    "risk_level, approval_summary, concerns, recommended_status. You may assess risk, "
    "but you cannot override deterministic tenant policy."
)

DEFAULT_OUTPUT_REVIEW_SYSTEM = (
    "You are the LLM output-review node for a governed enterprise agent. "
    "Return only valid JSON with keys: status, reason, findings. "
    "status must be approved, approved_with_warnings, failed, or skipped. "
    "Findings must be a list of objects with severity and message. "
    "Check whether the output is grounded in the plan, respects approval status, "
    "does not invent data, and is safe to return to the caller."
)

PLAN_REVIEW_STATUSES = {"approved", "approved_with_warnings", "rejected", "skipped"}
OUTPUT_REVIEW_STATUSES = {"approved", "approved_with_warnings", "failed", "skipped"}


def parse_review_status(
    *,
    data: dict[str, Any],
    deterministic: dict[str, Any],
    allowed_statuses: set[str],
    label: str,
) -> tuple[str, str, list[dict[str, str]]]:
    """Shared scaffolding for status-style reviewers (plan/output).

    Returns ``(status, reason, findings)`` after validating the LLM status
    against ``allowed_statuses``. Reviewer-specific normalization is applied by
    the caller.
    """
    status = str(data.get("status") or deterministic["status"])
    if status not in allowed_statuses:
        raise LLMRequiredError(f"{label} LLM returned invalid status: {status}")
    reason = str(data.get("reason") or deterministic["reason"])
    findings = _findings(data.get("findings"))
    return status, reason, findings


def promote_approved_with_findings(status: str, findings: list[dict[str, str]]) -> str:
    return "approved_with_warnings" if findings and status == "approved" else status


class PlanReviewer:
    """Review an execution plan before any skill or tool runs."""

    def __init__(
        self,
        tenant_config: dict[str, Any],
        *,
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        self._tenant_config = tenant_config
        self._prompts = prompt_library or PromptLibrary()

    def review(self, *, request: TaskRequest, plan: TaskPlan) -> dict[str, Any]:
        deterministic = self._deterministic_review(plan=plan)
        data = require_chat_json(
            self._llm_system_prompt(),
            self._llm_user_prompt(request=request, plan=plan, deterministic=deterministic),
        )
        return self._review_from_llm_data(data=data, deterministic=deterministic, plan=plan)

    def deterministic_review(self, *, plan: TaskPlan) -> dict[str, Any]:
        """Rule-based plan review only (no LLM). Used by the fast-path."""
        deterministic = self._deterministic_review(plan=plan)
        findings = list(deterministic.get("findings", []))
        status = promote_approved_with_findings(str(deterministic["status"]), findings)
        return {
            "status": status,
            "reason": deterministic["reason"],
            "findings": findings,
            "step_count": len(plan.steps),
            "llm_required": False,
        }

    def _deterministic_review(self, *, plan: TaskPlan) -> dict[str, Any]:
        if not plan.steps:
            return {
                "status": "skipped",
                "reason": "no business skill selected",
                "findings": [],
            }

        findings = [{"severity": "warning", "message": warning} for warning in plan.warnings]
        return {
            "status": "approved_with_warnings" if findings else "approved",
            "reason": "default no-op plan review",
            "findings": findings,
            "step_count": len(plan.steps),
        }

    def _llm_system_prompt(self) -> str:
        return self._prompts.system("plan_review", DEFAULT_PLAN_REVIEW_SYSTEM)

    def _llm_user_prompt(
        self,
        *,
        request: TaskRequest,
        plan: TaskPlan,
        deterministic: dict[str, Any],
    ) -> str:
        payload = {
            "message": request.text,
            "request_context": request.context,
            "roles": request.roles,
            "plan": asdict(plan),
            "tenant_policy": {
                "approval_required_skills": self._tenant_config.get("approval_required_skills", []),
                "role_permissions": self._tenant_config.get("role_permissions", {}),
            },
            "deterministic_review": deterministic,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _review_from_llm_data(
        self,
        *,
        data: dict[str, Any],
        deterministic: dict[str, Any],
        plan: TaskPlan,
    ) -> dict[str, Any]:
        status, reason, findings = parse_review_status(
            data=data,
            deterministic=deterministic,
            allowed_statuses=PLAN_REVIEW_STATUSES,
            label="Plan review",
        )
        if plan.warnings:
            findings.extend(
                {"severity": "warning", "message": warning} for warning in plan.warnings
            )
        if not plan.steps and status == "rejected":
            findings.append(
                {
                    "severity": "warning",
                    "message": (
                        "LLM plan review rejected an empty conversational plan; "
                        "normalized to skipped so conversation fallback can execute."
                    ),
                }
            )
            status = "skipped"
        status = promote_approved_with_findings(status, findings)
        return {
            "status": status,
            "reason": reason,
            "findings": findings,
            "step_count": len(plan.steps),
            "llm_required": True,
        }


class HumanApprovalGate:
    """Pause before execution when tenant policy requires human approval."""

    def __init__(
        self,
        tenant_config: dict[str, Any],
        *,
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        self._tenant_config = tenant_config
        self._prompts = prompt_library or PromptLibrary()

    def evaluate(
        self,
        *,
        request: TaskRequest,
        plan: TaskPlan,
        plan_review: dict[str, Any],
        skip_llm_assessment: bool = False,
    ) -> dict[str, Any]:
        decision = self._deterministic_decision(
            request=request,
            plan=plan,
            plan_review=plan_review,
        )
        # When the human has already decided (resume / resubmit with an explicit
        # approve/reject), or the deterministic fast-path is active, the advisory
        # LLM assessment adds latency without value, so skip it.
        human_decided = bool(
            request.context.get("approved_skills") or request.context.get("rejected_skills")
        )
        llm_used = not (human_decided or skip_llm_assessment)
        if not llm_used:
            summary = (
                "human decision provided; LLM assessment skipped"
                if human_decided
                else "deterministic fast-path; LLM assessment skipped"
            )
            decision["llm_assessment"] = {
                "risk_level": "low",
                "approval_summary": summary,
                "concerns": [],
                "recommended_status": decision["status"],
            }
        else:
            decision["llm_assessment"] = self._llm_assessment(
                request=request,
                plan=plan,
                plan_review=plan_review,
                deterministic_decision=decision,
            )
        decision["llm_required"] = llm_used
        decision["llm_assessment_used"] = llm_used
        return decision

    def _deterministic_decision(
        self,
        *,
        request: TaskRequest,
        plan: TaskPlan,
        plan_review: dict[str, Any],
    ) -> dict[str, Any]:
        if plan_review.get("status") == "rejected":
            return {
                "status": "rejected",
                "required": False,
                "skills": [step.skill_name for step in plan.steps],
                "reason": "plan review rejected execution before skill invocation",
            }

        view = evaluate_approval(
            planned_skills=[step.skill_name for step in plan.steps],
            approval_required_skills=self._tenant_config.get("approval_required_skills", []),
            approved_skills=request.context.get("approved_skills", []),
            rejected_skills=request.context.get("rejected_skills", []),
        )

        if view.rejected:
            return {
                "status": "rejected",
                "required": True,
                "skills": view.rejected,
                "reason": "human rejected execution before skill invocation",
            }

        if view.pending:
            return {
                "status": "waiting_for_approval",
                "required": True,
                "skills": view.pending,
                "reason": "tenant policy requires approval before execution",
                "resume_hint": (
                    "Add the skill name to request.context.approved_skills and resume the run."
                ),
            }

        return {
            "status": "approved",
            "required": False,
            "skills": [],
            "reason": "no human approval required",
        }

    def _llm_assessment(
        self,
        *,
        request: TaskRequest,
        plan: TaskPlan,
        plan_review: dict[str, Any],
        deterministic_decision: dict[str, Any],
    ) -> dict[str, Any]:
        data = require_chat_json(
            self._prompts.system("approval", DEFAULT_APPROVAL_SYSTEM),
            json.dumps(
                {
                    "message": request.text,
                    "roles": request.roles,
                    "context": request.context,
                    "plan": asdict(plan),
                    "plan_review": plan_review,
                    "tenant_approval_required_skills": self._tenant_config.get(
                        "approval_required_skills", []
                    ),
                    "deterministic_decision": deterministic_decision,
                },
                ensure_ascii=False,
                default=str,
            ),
        )
        concerns = data.get("concerns")
        if not isinstance(concerns, list):
            concerns = []
        risk_level = str(data.get("risk_level") or "medium")
        if risk_level not in {"low", "medium", "high"}:
            risk_level = "medium"
        return {
            "risk_level": risk_level,
            "approval_summary": str(data.get("approval_summary") or ""),
            "concerns": [str(item) for item in concerns[:8]],
            "recommended_status": str(data.get("recommended_status") or ""),
        }


class OutputReviewer:
    """Review final output before returning it to the caller."""

    def __init__(
        self,
        tenant_config: dict[str, Any],
        *,
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        self._tenant_config = tenant_config
        self._prompts = prompt_library or PromptLibrary()

    def review(
        self,
        *,
        request: TaskRequest,
        plan: TaskPlan,
        output: dict[str, Any],
    ) -> dict[str, Any]:
        deterministic = self._deterministic_review(output=output)
        data = require_chat_json(
            self._llm_system_prompt(),
            self._llm_user_prompt(
                request=request,
                plan=plan,
                output=output,
                deterministic=deterministic,
            ),
        )
        return self._review_from_llm_data(data=data, deterministic=deterministic, output=output)

    def _deterministic_review(self, *, output: dict[str, Any]) -> dict[str, Any]:
        if output.get("error"):
            return {
                "status": "failed",
                "reason": str(output.get("reason") or output["error"]),
                "findings": [{"severity": "error", "message": "execution returned an error"}],
            }

        if output.get("status") == "waiting_for_approval":
            return {
                "status": "skipped",
                "reason": "execution is waiting for human approval",
                "findings": [],
            }

        if output.get("status") == "rejected":
            return {
                "status": "skipped",
                "reason": "execution was rejected before skill invocation",
                "findings": [],
            }

        if "final" not in output:
            return {
                "status": "approved_with_warnings",
                "reason": "output has no final payload",
                "findings": [{"severity": "warning", "message": "missing final output payload"}],
            }

        return {
            "status": "approved",
            "reason": "default no-op output review",
            "findings": [],
        }

    def _llm_system_prompt(self) -> str:
        return self._prompts.system("output_review", DEFAULT_OUTPUT_REVIEW_SYSTEM)

    def _llm_user_prompt(
        self,
        *,
        request: TaskRequest,
        plan: TaskPlan,
        output: dict[str, Any],
        deterministic: dict[str, Any],
    ) -> str:
        payload = {
            "message": request.text,
            "request_context": request.context,
            "roles": request.roles,
            "plan": asdict(plan),
            "output": output,
            "deterministic_review": deterministic,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    def _review_from_llm_data(
        self,
        *,
        data: dict[str, Any],
        deterministic: dict[str, Any],
        output: dict[str, Any],
    ) -> dict[str, Any]:
        status, reason, findings = parse_review_status(
            data=data,
            deterministic=deterministic,
            allowed_statuses=OUTPUT_REVIEW_STATUSES,
            label="Output review",
        )
        if output.get("error"):
            status = "failed"
        if output.get("status") in {"waiting_for_approval", "rejected"}:
            status = "skipped"
        if deterministic.get("findings"):
            findings.extend(deterministic["findings"])
        if status == "failed" and not output.get("error") and output.get("final"):
            policy = str(self._tenant_config.get("output_review_policy", "warn")).lower()
            if policy in {"block", "block_on_failed", "fail_closed"}:
                findings.append(
                    {
                        "severity": "error",
                        "message": "LLM output review blocked the final payload.",
                    }
                )
                return {
                    "status": "failed",
                    "reason": reason,
                    "findings": findings,
                    "llm_required": True,
                    "enforcement": "blocked",
                }
            findings.append(
                {
                    "severity": "warning",
                    "message": (
                        "LLM output review requested failed status, but execution returned "
                        "a final payload without an error; normalized to warning."
                    ),
                }
            )
            status = "approved_with_warnings"
        status = promote_approved_with_findings(status, findings)
        return {
            "status": status,
            "reason": reason,
            "findings": findings,
            "llm_required": True,
        }


def _findings(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    findings: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            severity = str(item.get("severity") or "info")
            message = str(item.get("message") or "").strip()
        else:
            severity = "info"
            message = str(item).strip()
        if message:
            findings.append({"severity": severity, "message": message})
    return findings
