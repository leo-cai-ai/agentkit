"""Flask management console for AgentKit.

Run from the repository root:

    agentkit web
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from agentkit.config import get_settings
from agentkit.core.audit import SQLiteAuditLog
from agentkit.core.contracts import TaskRequest
from agentkit.core.identity import (
    CHAT_USE,
    GOVERNANCE_VIEW,
    RUNS_VIEW,
    RUNTIME_ADMIN,
    TASK_APPROVE,
    TASK_RUN,
    has_permission,
    load_role_permissions,
)
from agentkit.runtime.bootstrap import AGENTKIT_ROOT, build_runtime, resolve_tenant_id
from agentkit.web.identity import current_principal, require_permission
from agentkit.web.security import configure_security
from agentkit.web.streaming import stream_response

# Which context inputs each agent actually consumes. Agents not listed here are
# treated as conversational (no structured inputs, driven purely by the prompt).
AGENT_CONTEXT_FIELDS = {
    "hr_recruiter": ["job_id", "top_n", "candidate_ids"],
}

DEFAULT_UI_CONFIG = {
    "demo_prompt": "Rank the top 3 candidates for JOB-001 and explain why.",
    "default_agent": "hr_recruiter",
    "chat_agents": [
        {
            "name": "hr_recruiter",
            "label": "HR Recruiter Agent",
            "domain": "hr.recruitment",
            "mission": "Candidate ranking and recruitment decisions",
            "status": "online",
            "allowed_skills": ["candidate.rank"],
            "allowed_tools": ["ats.get_job", "ats.get_candidates"],
            "fields": ["job_id", "top_n", "candidate_ids"],
            "mode": "chat",
            "actions_enabled": True,
        }
    ],
    "demo_prompts": {
        "hr_recruiter": "Rank the top 3 candidates for JOB-001 and explain why.",
        "customer_service": "我上周买的东西到现在还没收到，能帮我看看物流到哪了吗？",
    },
    "default_user_id": "u-001",
    "available_roles": ["recruiter", "hr_admin", "employee"],
    "default_roles": ["recruiter"],
    "job_requisitions": ["JOB-001"],
    "default_job_id": "JOB-001",
    "candidate_pool": ["C-100", "C-101", "C-102", "C-103", "C-104"],
    "default_candidate_ids": ["C-100", "C-101", "C-102", "C-103", "C-104"],
    "min_top_n": 1,
    "max_top_n": 5,
    "default_top_n": 3,
}

app = Flask(__name__)
# Pick up template/static edits without a manual restart during local development.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# Token auth, CSRF protection, secure cookies, and security headers.
configure_security(app)


@lru_cache(maxsize=8)
def _build_runtime_cached(tenant_id: str):
    return build_runtime(tenant_id=tenant_id)


def get_runtime():
    # Tenant id resolves from $AGENTKIT_TENANT_ID (or the default), so the
    # console can be pointed at any tenant without code changes. Runtimes are
    # cached per tenant id.
    return _build_runtime_cached(resolve_tenant_id())


def clear_runtime_cache() -> None:
    """Drop cached runtimes so tenant configs, prompts, and packs reload."""
    _build_runtime_cached.cache_clear()


@app.get("/api/xhs/publish-assets/<path:filename>")
@require_permission(CHAT_USE)
def api_xhs_publish_asset(filename: str):
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.lower().endswith((".png", ".jpg", ".jpeg")):
        return jsonify({"error": "invalid publish asset path"}), 400
    runtime = get_runtime()
    social_config = runtime.tenant_config.get("social_growth", {})
    root = (
        social_config.get("publish_asset_root") if isinstance(social_config, dict) else None
    ) or get_settings().xhs_publish_asset_root
    return send_from_directory(Path(str(root)).expanduser().resolve(), safe_name)


@app.context_processor
def inject_global_context() -> dict[str, Any]:
    runtime = get_runtime()
    ui = get_ui_config(runtime.tenant_config)
    return {
        "tenant_id": runtime.tenant_config["tenant_id"],
        "db_path": display_path(runtime.db_path),
        "demo_prompt": ui["demo_prompt"],
    }


@app.template_filter("format_cell")
def format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list | tuple | set):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


@app.template_filter("json_pretty")
def json_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


@app.template_filter("format_ts")
def format_ts_filter(value: Any) -> str:
    return format_timestamp(value)


@app.get("/")
def overview():
    runtime = get_runtime()
    gateway = runtime.gateway
    audit = gateway.audit
    counts = _safe_counts(audit)
    completed = counts.get("completed", 0)
    running = counts.get("running", 0)
    failed = counts.get("failed", 0)
    resolved = completed + failed
    success_rate = round((completed / resolved) * 100, 1) if resolved else 100.0
    batch_tools = sum(1 for tool in gateway.tools.all() if tool.supports_batch)
    runs = _safe_runs(audit, limit=8)

    metrics = [
        {"label": "Runs Completed", "value": completed, "helper": "Durable audit"},
        {"label": "Success Rate", "value": f"{success_rate}%", "helper": "Latest environment"},
        {"label": "Active Runs", "value": running, "helper": "Currently executing"},
        {
            "label": "Skills Online",
            "value": len(gateway.skills.all()),
            "helper": "Business capabilities",
        },
        {"label": "Batch Tools", "value": batch_tools, "helper": "Scale-ready APIs"},
    ]
    impact_rows = [
        {
            "Area": "Recruiting",
            "Agent": "Recruitment Intelligence",
            "Status": "Online",
            "Impact": "Candidate ranking and shortlisting",
        },
        {
            "Area": "Onboarding",
            "Agent": "Onboarding Coordinator",
            "Status": "Planned",
            "Impact": "Checklist and department readiness",
        },
        {
            "Area": "Training",
            "Agent": "Training Producer",
            "Status": "Planned",
            "Impact": "Learning material generation",
        },
        {
            "Area": "HR Ops",
            "Agent": "Policy Assistant",
            "Status": "Planned",
            "Impact": "Policy Q&A and monthly reporting",
        },
    ]
    agent_rows = [
        {
            "Agent": agent.name,
            "Domain": agent.domain,
            "Health": "Working" if running else "Ready",
            "Skills": len(agent.allowed_skills),
        }
        for agent in gateway.agents.all()
    ]
    capabilities = [
        {
            "name": "Recruitment",
            "status": "Live",
            "description": "Candidate ranking, ATS lookup, batch evaluation.",
        },
        {
            "name": "Governance",
            "status": "Live",
            "description": "Role permissions, audit events, SQLite run history.",
        },
        {
            "name": "Scale Execution",
            "status": "Live",
            "description": "Batch executor with configurable shard size.",
        },
    ]
    return render_template(
        "overview.html",
        active="overview",
        title="Management Dashboard",
        metrics=metrics,
        impact_rows=impact_rows,
        agent_rows=agent_rows,
        runs=runs,
        capabilities=capabilities,
    )


@app.get("/healthz")
def healthz():
    # Lightweight liveness probe: public, no auth, no LLM, no runtime build.
    return jsonify({"status": "ok"})


@app.post("/api/admin/reload")
@require_permission(RUNTIME_ADMIN)
def api_admin_reload():
    """Reload settings, LLM provider, and cached tenant runtimes."""
    from agentkit.config import get_settings
    from agentkit.core.llm_client import clear_provider_cache

    get_settings.cache_clear()
    clear_provider_cache()
    clear_runtime_cache()
    return jsonify({"status": "reloaded", "tenant_id": resolve_tenant_id()})


@app.get("/chat")
def chat_console():
    runtime = get_runtime()
    return render_template(
        "chat.html",
        active="chat",
        title="Chat Console",
        ui=get_ui_config(runtime.tenant_config),
        chat_agents=get_chat_agents(runtime),
    )


@app.get("/operations")
def operations():
    runtime = get_runtime()
    audit = runtime.gateway.audit
    counts = _safe_counts(audit)
    completed = counts.get("completed", 0)
    running = counts.get("running", 0)
    failed = counts.get("failed", 0)
    blocked = counts.get("waiting_for_approval", 0) + counts.get("rejected", 0)
    total = sum(counts.values())
    runs = _safe_runs(audit, limit=50)
    selected_run_id = request.args.get("run_id") or (runs[0]["run_id"] if runs else "")
    events = (
        audit.events_for(selected_run_id)
        if isinstance(audit, SQLiteAuditLog) and selected_run_id
        else []
    )

    metrics = [
        {"label": "Total Runs", "value": total, "helper": "Recorded executions"},
        {"label": "Completed", "value": completed, "helper": "Finished successfully"},
        {"label": "Running", "value": running, "helper": "Currently active"},
        {"label": "Blocked", "value": blocked, "helper": "Awaiting or rejected approval"},
        {"label": "Failed", "value": failed, "helper": "Requires attention"},
    ]
    event_rows = [
        {
            "Time": format_timestamp(event["ts"]),
            "Event": event["type"],
            "Details": json.dumps(event["payload"], ensure_ascii=False),
        }
        for event in events
    ]
    return render_template(
        "operations.html",
        active="operations",
        title="Operations Monitor",
        metrics=metrics,
        runs=runs,
        selected_run_id=selected_run_id,
        event_rows=event_rows,
    )


@app.get("/governance")
@require_permission(GOVERNANCE_VIEW)
def governance():
    runtime = get_runtime()
    gateway = runtime.gateway
    audit = gateway.audit
    agents = [
        {
            "Name": agent.name,
            "Domain": agent.domain,
            "Model": agent.model,
            "Prompt File": agent.prompt_file,
            "Allowed Skills": ", ".join(agent.allowed_skills),
            "Description": agent.description,
        }
        for agent in gateway.agents.all()
    ]
    skills = [
        {
            "Name": skill.name,
            "Domain": skill.domain,
            "Mode": skill.execution_mode,
            "Skill File": skill.skill_file,
            "Scripts": len(skill.skill_resources.get("scripts", [])),
            "References": len(skill.skill_resources.get("references", [])),
            "Permissions": ", ".join(skill.permissions),
            "Tools": ", ".join(skill.tools),
        }
        for skill in gateway.skills.all()
    ]
    tools = [
        {
            "Name": tool.name,
            "Domain": tool.domain,
            "Batch": tool.supports_batch,
            "Description": tool.description,
        }
        for tool in gateway.tools.all()
    ]
    event_counts = audit.event_counts_by_type() if isinstance(audit, SQLiteAuditLog) else []
    cost_summary = audit.cost_summary() if isinstance(audit, SQLiteAuditLog) else {}
    prompt_rows = [
        {"Name": name, "File": path}
        for name, path in runtime.tenant_config.get("prompt_files", {}).items()
    ]
    return render_template(
        "governance.html",
        active="governance",
        title="Governance",
        agents=agents,
        skills=skills,
        tools=tools,
        prompt_rows=prompt_rows,
        event_counts=event_counts,
        cost_summary=cost_summary,
    )


def _effective_user_id(payload: dict, ui: dict) -> str:
    """Attribute the request to the authenticated principal when available."""
    principal = current_principal()
    if principal.is_authenticated:
        return principal.subject
    return str(payload.get("user_id") or ui["default_user_id"])


def _trusted_business_roles(
    *,
    tenant_config: dict[str, Any],
    ui: dict[str, Any],
) -> tuple[list[str], str]:
    """Resolve tenant business roles from trusted identity/config, never payload."""
    principal = current_principal()
    role_permissions = tenant_config.get("role_permissions", {})
    known_business_roles = set(role_permissions) if isinstance(role_permissions, dict) else set()

    business_claims = _list_or_default(principal.claims.get("business_roles"), [])
    claim_roles = [role for role in business_claims if role in known_business_roles]
    if claim_roles:
        return claim_roles, "principal.claims.business_roles"

    role_mapping = tenant_config.get("principal_business_roles", {})
    if isinstance(role_mapping, dict):
        mapped: list[str] = []
        for role in principal.roles:
            for business_role in _list_or_default(role_mapping.get(role), []):
                if business_role in known_business_roles and business_role not in mapped:
                    mapped.append(business_role)
        if mapped:
            return mapped, "tenant.principal_business_roles"

    # Some proxies send business roles and console roles in the same header. Treat
    # only roles that exist in tenant role_permissions as business roles.
    overlapping = [str(role) for role in principal.roles if str(role) in known_business_roles]
    if overlapping:
        return overlapping, "principal.roles"

    # Local/shared-token deployments stay usable without trusting browser input.
    # Production SSO should prefer business role claims or tenant mapping above.
    if principal.auth_method == "proxy":
        return [], "none"
    fallback = [
        role
        for role in _list_or_default(ui.get("default_roles"), [])
        if not known_business_roles or role in known_business_roles
    ]
    return fallback, "tenant.ui.default_roles"


def _approval_context_from_payload(
    payload: dict[str, Any],
    *,
    allow_approval_context: bool,
) -> tuple[list[str], list[str]]:
    approved_skills = _list_or_default(payload.get("approved_skills"), [])
    rejected_skills = _list_or_default(payload.get("rejected_skills"), [])
    if (approved_skills or rejected_skills) and not allow_approval_context:
        raise ValueError(
            "approval decisions are not accepted on /api/tasks; use /api/tasks/resume "
            "or /api/tasks/approve."
        )
    overlap = sorted(set(approved_skills) & set(rejected_skills))
    if overlap:
        raise ValueError(
            "approval decision cannot both approve and reject the same skills: "
            + ", ".join(overlap)
        )
    return approved_skills, rejected_skills


def _approval_decision_context(
    *,
    approved_skills: list[str],
    rejected_skills: list[str],
    source: str,
) -> dict[str, Any]:
    if approved_skills and rejected_skills:
        action = "mixed"
    elif rejected_skills:
        action = "reject"
    else:
        action = "approve"
    return {
        "source": source,
        "action": action,
        "principal": current_principal().to_public_dict(),
        "approved_skills": list(approved_skills),
        "rejected_skills": list(rejected_skills),
    }


def _task_request_from_payload(
    payload: dict,
    *,
    tenant_config: dict[str, Any],
    ui: dict[str, Any],
    allowed_agents: set[str] | None = None,
    allow_approval_context: bool = False,
) -> TaskRequest:
    """Build a ``TaskRequest`` from a web payload (raises ``ValueError`` on bad input)."""
    text = str(payload.get("text") or ui["demo_prompt"])
    user_id = _effective_user_id(payload, ui)
    roles, roles_source = _trusted_business_roles(tenant_config=tenant_config, ui=ui)
    agent = str(payload.get("agent") or ui.get("default_agent") or "")
    if allowed_agents is not None and agent not in allowed_agents:
        allowed = ", ".join(sorted(allowed_agents)) or "(none)"
        raise ValueError(f"agent '{agent}' is not an enabled action agent. Allowed: {allowed}.")
    approved_skills, rejected_skills = _approval_context_from_payload(
        payload,
        allow_approval_context=allow_approval_context,
    )

    context: dict[str, Any] = {"agent": agent}
    if payload.get("job_id") is not None and payload.get("job_id") != "":
        context["job_id"] = str(payload["job_id"])
    if "candidate_ids" in payload:
        context["candidate_ids"] = _list_or_default(payload.get("candidate_ids"), [])
    if payload.get("top_n") is not None and payload.get("top_n") != "":
        try:
            context["top_n"] = int(payload["top_n"])
        except (TypeError, ValueError) as exc:
            raise ValueError("top_n must be an integer.") from exc
    extra_context = payload.get("context")
    if isinstance(extra_context, dict):
        reserved_context_keys = {
            "agent",
            "approval",
            "approval_decision",
            "approved_skills",
            "business_roles_source",
            "candidate_ids",
            "principal",
            "rejected_skills",
            "roles",
            "safety",
            "top_n",
            "user_id",
        }
        for key, value in extra_context.items():
            key = str(key)
            if key not in reserved_context_keys:
                context[key] = value
    if "roles" in payload:
        context["ignored_payload_roles"] = _list_or_default(payload.get("roles"), [])
    if approved_skills:
        context["approved_skills"] = approved_skills
    if rejected_skills:
        context["rejected_skills"] = rejected_skills
    if approved_skills or rejected_skills:
        context["approval_decision"] = _approval_decision_context(
            approved_skills=approved_skills,
            rejected_skills=rejected_skills,
            source="web-resubmit",
        )
    context["principal"] = current_principal().to_public_dict()
    context["business_roles_source"] = roles_source
    return TaskRequest(user_id=user_id, roles=roles, text=text, context=context)


def _sse(generator: Any) -> Response:
    """Wrap an SSE frame generator in a streaming Flask response."""
    response = Response(generator, mimetype="text/event-stream")
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


def _permission_denied(permission: str):
    mapping = load_role_permissions(get_settings())
    if has_permission(current_principal(), permission, mapping):
        return None
    return jsonify({"error": f"Forbidden: requires permission '{permission}'."}), 403


def _payload_context(payload: dict[str, Any]) -> dict[str, Any]:
    context = payload.get("context")
    return dict(context) if isinstance(context, dict) else {}


def _chat_agent_from_payload(
    payload: dict[str, Any],
    *,
    context: dict[str, Any],
    ui: dict[str, Any],
) -> str:
    return str(
        context.get("agent")
        or context.get("agent_name")
        or payload.get("agent")
        or ui.get("default_agent")
        or ""
    )


def _chat_message_from_payload(
    payload: dict[str, Any],
    *,
    context: dict[str, Any],
) -> str:
    return str(
        context.get("message")
        or context.get("input")
        or context.get("text")
        or payload.get("message")
        or payload.get("input")
        or payload.get("text")
        or ""
    ).strip()


def _chat_conversation_id(
    payload: dict[str, Any],
    *,
    context: dict[str, Any],
) -> str | None:
    conversation_id = context.get("conversation_id") or payload.get("conversation_id")
    return str(conversation_id) if conversation_id else None


def _chat_approval_from_payload(
    payload: dict[str, Any],
    *,
    context: dict[str, Any],
) -> dict[str, Any] | None:
    raw = context.get("approval", payload.get("approval"))
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("context.approval must be an object.")

    action = str(raw.get("action") or "").strip().lower()
    if action not in {"approve", "reject"}:
        raise ValueError("context.approval.action must be 'approve' or 'reject'.")

    skills = _list_or_default(raw.get("skills"), [])
    if not skills:
        decision_key = "approved_skills" if action == "approve" else "rejected_skills"
        skills = _list_or_default(raw.get(decision_key), [])
    if not skills:
        raise ValueError("context.approval.skills is required.")

    request_payload = raw.get("request")
    if request_payload is not None and not isinstance(request_payload, dict):
        raise ValueError("context.approval.request must be an object when provided.")

    return {
        "action": action,
        "thread_id": str(raw.get("thread_id") or "").strip(),
        "skills": skills,
        "request": dict(request_payload) if isinstance(request_payload, dict) else None,
    }


def _task_payload_from_chat_payload(
    payload: dict[str, Any],
    *,
    agent: str,
    message: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    task_payload: dict[str, Any] = {
        "agent": agent,
        "text": message,
        "context": {
            key: value
            for key, value in context.items()
            if key
            not in {
                "agent",
                "agent_name",
                "approval",
                "conversation_id",
                "input",
                "message",
                "text",
            }
        },
    }
    if "user_id" in payload:
        task_payload["user_id"] = payload["user_id"]
    for field in ("job_id", "candidate_ids", "top_n", "skill"):
        if field in context:
            task_payload[field] = context[field]
        elif field in payload:
            task_payload[field] = payload[field]
    return task_payload


def _format_action_chat_result(
    response: dict[str, Any],
    *,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    result = {
        "mode": "action",
        "interaction_mode": "chat",
        "agent_kind": "action",
        "assistant_text": format_chat_response(response),
        "response": response,
    }
    if conversation_id:
        result["conversation_id"] = conversation_id
    return result


def _action_chat_runner(
    *,
    payload: dict[str, Any],
    runtime: Any,
    ui: dict[str, Any],
    agent: str,
    message: str,
    context: dict[str, Any],
    approval: dict[str, Any] | None = None,
) -> tuple[Callable[[], dict[str, Any]], str | None]:
    chat_service = getattr(runtime, "chat_service", None)
    user_id = _effective_user_id(payload, ui)
    conversation_id = _chat_conversation_id(payload, context=context)
    action_memory: dict[str, Any] | None = None
    if chat_service is not None:
        roles, _roles_source = _trusted_business_roles(
            tenant_config=runtime.tenant_config,
            ui=ui,
        )
        action_context = chat_service.prepare_action_turn(
            agent=agent,
            user_id=user_id,
            message=message,
            conversation_id=conversation_id,
            roles=roles,
        )
        conversation_id = str(action_context["conversation_id"])
        action_memory = action_context["memory"]

    task_payload = _task_payload_from_chat_payload(
        payload,
        agent=agent,
        message=message,
        context=context,
    )
    task_payload.setdefault("context", {})
    task_payload["context"]["conversation_id"] = conversation_id
    if action_memory is not None:
        task_payload["context"]["chat_memory"] = action_memory
    allowed_agents = _action_agent_names(runtime)

    def record_action_result(
        response: dict[str, Any],
        *,
        user_message: str | None,
    ) -> dict[str, Any]:
        result = _format_action_chat_result(response, conversation_id=conversation_id)
        if chat_service is not None and conversation_id:
            try:
                chat_service.record_action_turn(
                    agent=agent,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    user_message=user_message,
                    assistant_text=result["assistant_text"],
                    run_id=str(response.get("output", {}).get("run_id") or ""),
                )
            except ValueError:
                # Conversation persistence must not hide a successful governed run.
                pass
        return result

    def run_and_record_failure(
        operation: Callable[[], dict[str, Any]],
        *,
        user_message: str | None,
    ) -> dict[str, Any]:
        try:
            return operation()
        except Exception as exc:
            if chat_service is not None and conversation_id:
                error_text = str(exc).strip() or exc.__class__.__name__
                try:
                    chat_service.record_action_turn(
                        agent=agent,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        user_message=user_message,
                        assistant_text=f"Execution failed: {error_text}",
                        extract_memories=False,
                    )
                except Exception:  # noqa: BLE001 - preserve the original execution error
                    # Persistence failure must not replace the original execution error.
                    pass
            raise

    if approval:
        approved_skills = approval["skills"] if approval["action"] == "approve" else []
        rejected_skills = approval["skills"] if approval["action"] == "reject" else []
        if approval["thread_id"]:
            decision_context = _approval_decision_context(
                approved_skills=approved_skills,
                rejected_skills=rejected_skills,
                source="chat-resume",
            )

            def run_resume() -> dict[str, Any]:
                def resume() -> dict[str, Any]:
                    response = runtime.gateway.resume(
                        approval["thread_id"],
                        approved_skills=approved_skills,
                        rejected_skills=rejected_skills,
                        decision_context=decision_context,
                    ).to_dict()
                    return record_action_result(response, user_message=message)

                return run_and_record_failure(resume, user_message=message)

            return run_resume, conversation_id
        else:
            resubmit_payload = approval["request"] or task_payload
            request_context = _payload_context(resubmit_payload)
            resubmit_agent = _chat_agent_from_payload(
                resubmit_payload,
                context=request_context,
                ui=ui,
            )
            resubmit_message = _chat_message_from_payload(
                resubmit_payload,
                context=request_context,
            )
            task_payload = _task_payload_from_chat_payload(
                resubmit_payload,
                agent=resubmit_agent or agent,
                message=resubmit_message or message,
                context=request_context or context,
            )
            task_payload.setdefault("context", {})
            task_payload["context"]["conversation_id"] = conversation_id
            if action_memory is not None:
                task_payload["context"]["chat_memory"] = action_memory
            if approved_skills:
                task_payload["approved_skills"] = approved_skills
            if rejected_skills:
                task_payload["rejected_skills"] = rejected_skills
            task_request = _task_request_from_payload(
                task_payload,
                tenant_config=runtime.tenant_config,
                ui=ui,
                allowed_agents=allowed_agents,
                allow_approval_context=True,
            )

            def run_resubmit() -> dict[str, Any]:
                def resubmit() -> dict[str, Any]:
                    response = runtime.gateway.handle(task_request).to_dict()
                    return record_action_result(response, user_message=message)

                return run_and_record_failure(resubmit, user_message=message)

            return run_resubmit, conversation_id
    else:
        task_request = _task_request_from_payload(
            task_payload,
            tenant_config=runtime.tenant_config,
            ui=ui,
            allowed_agents=allowed_agents,
        )

        def run_task() -> dict[str, Any]:
            def handle() -> dict[str, Any]:
                response = runtime.gateway.handle(task_request).to_dict()
                return record_action_result(response, user_message=message)

            return run_and_record_failure(handle, user_message=message)

        return run_task, conversation_id


def _action_chat_result(
    *,
    payload: dict[str, Any],
    runtime: Any,
    ui: dict[str, Any],
    agent: str,
    message: str,
    context: dict[str, Any],
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runner, _conversation_id = _action_chat_runner(
        payload=payload,
        runtime=runtime,
        ui=ui,
        agent=agent,
        message=message,
        context=context,
        approval=approval,
    )
    return runner()


def _action_chat_response(
    *,
    payload: dict[str, Any],
    runtime: Any,
    ui: dict[str, Any],
    agent: str,
    message: str,
    context: dict[str, Any],
    approval: dict[str, Any] | None = None,
):
    denied = _permission_denied(TASK_APPROVE if approval else TASK_RUN)
    if denied is not None:
        return denied
    try:
        result = _action_chat_result(
            payload=payload,
            runtime=runtime,
            ui=ui,
            agent=agent,
            message=message,
            context=context,
            approval=approval,
        )
    except KeyError:
        return jsonify({"error": "This approval session expired. Please resubmit the task."}), 409
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.post("/api/tasks")
@require_permission(TASK_RUN)
def create_task():
    payload = request.get_json(silent=True) or {}
    runtime = get_runtime()
    ui = get_ui_config(runtime.tenant_config)
    try:
        task_request = _task_request_from_payload(
            payload,
            tenant_config=runtime.tenant_config,
            ui=ui,
            allowed_agents=_action_agent_names(runtime),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    response = runtime.gateway.handle(task_request).to_dict()
    return jsonify(
        {
            "assistant_text": format_chat_response(response),
            "response": response,
        }
    )


@app.post("/api/tasks/stream")
@require_permission(TASK_RUN)
def create_task_stream():
    payload = request.get_json(silent=True) or {}
    runtime = get_runtime()
    ui = get_ui_config(runtime.tenant_config)
    try:
        task_request = _task_request_from_payload(
            payload,
            tenant_config=runtime.tenant_config,
            ui=ui,
            allowed_agents=_action_agent_names(runtime),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    def produce() -> dict[str, Any]:
        response = runtime.gateway.handle(task_request).to_dict()
        return {
            "assistant_text": format_chat_response(response),
            "response": response,
        }

    return _sse(
        stream_response(
            produce,
            stream_tokens=_task_stream_tokens_enabled(runtime.tenant_config),
        )
    )


@app.post("/api/tasks/approve")
@require_permission(TASK_APPROVE)
def approve_task_resubmit():
    """Approve/reject by resubmitting a full task; requires explicit approve RBAC.

    This is only for deployments that disabled approval checkpointing. Normal
    approval should use /api/tasks/resume so planning is not recomputed.
    """
    payload = request.get_json(silent=True) or {}
    runtime = get_runtime()
    ui = get_ui_config(runtime.tenant_config)
    if not payload.get("approved_skills") and not payload.get("rejected_skills"):
        return jsonify({"error": "approved_skills or rejected_skills is required."}), 400
    try:
        task_request = _task_request_from_payload(
            payload,
            tenant_config=runtime.tenant_config,
            ui=ui,
            allowed_agents=_action_agent_names(runtime),
            allow_approval_context=True,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    response = runtime.gateway.handle(task_request).to_dict()
    return jsonify(
        {
            "assistant_text": format_chat_response(response),
            "response": response,
        }
    )


@app.post("/api/tasks/approve/stream")
@require_permission(TASK_APPROVE)
def approve_task_resubmit_stream():
    """Streaming approve/reject full resubmit for no-checkpointer deployments."""
    payload = request.get_json(silent=True) or {}
    runtime = get_runtime()
    ui = get_ui_config(runtime.tenant_config)
    if not payload.get("approved_skills") and not payload.get("rejected_skills"):
        return jsonify({"error": "approved_skills or rejected_skills is required."}), 400
    try:
        task_request = _task_request_from_payload(
            payload,
            tenant_config=runtime.tenant_config,
            ui=ui,
            allowed_agents=_action_agent_names(runtime),
            allow_approval_context=True,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    def produce() -> dict[str, Any]:
        response = runtime.gateway.handle(task_request).to_dict()
        return {
            "assistant_text": format_chat_response(response),
            "response": response,
        }

    return _sse(
        stream_response(
            produce,
            stream_tokens=_task_stream_tokens_enabled(runtime.tenant_config),
        )
    )


@app.post("/api/tasks/resume")
@require_permission(TASK_APPROVE)
def resume_task():
    payload = request.get_json(silent=True) or {}
    thread_id = str(payload.get("thread_id") or "").strip()
    if not thread_id:
        return jsonify({"error": "thread_id is required."}), 400
    approved_skills = _list_or_default(payload.get("approved_skills"), [])
    rejected_skills = _list_or_default(payload.get("rejected_skills"), [])
    if not approved_skills and not rejected_skills:
        return jsonify({"error": "approved_skills or rejected_skills is required."}), 400
    runtime = get_runtime()
    decision_context = _approval_decision_context(
        approved_skills=approved_skills,
        rejected_skills=rejected_skills,
        source="web-resume",
    )
    try:
        response = runtime.gateway.resume(
            thread_id,
            approved_skills=approved_skills,
            rejected_skills=rejected_skills,
            decision_context=decision_context,
        ).to_dict()
    except KeyError:
        # Thread expired/unknown (e.g. server restart with in-memory checkpointer).
        return jsonify({"error": "This approval session expired. Please resubmit the task."}), 409
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(
        {
            "assistant_text": format_chat_response(response),
            "response": response,
        }
    )


@app.post("/api/tasks/resume/stream")
@require_permission(TASK_APPROVE)
def resume_task_stream():
    payload = request.get_json(silent=True) or {}
    thread_id = str(payload.get("thread_id") or "").strip()
    if not thread_id:
        return jsonify({"error": "thread_id is required."}), 400
    approved_skills = _list_or_default(payload.get("approved_skills"), [])
    rejected_skills = _list_or_default(payload.get("rejected_skills"), [])
    if not approved_skills and not rejected_skills:
        return jsonify({"error": "approved_skills or rejected_skills is required."}), 400
    runtime = get_runtime()
    decision_context = _approval_decision_context(
        approved_skills=approved_skills,
        rejected_skills=rejected_skills,
        source="web-resume",
    )

    def produce() -> dict[str, Any]:
        try:
            response = runtime.gateway.resume(
                thread_id,
                approved_skills=approved_skills,
                rejected_skills=rejected_skills,
                decision_context=decision_context,
            ).to_dict()
        except KeyError as exc:
            raise ValueError("This approval session expired. Please resubmit the task.") from exc
        return {
            "assistant_text": format_chat_response(response),
            "response": response,
        }

    return _sse(
        stream_response(
            produce,
            stream_tokens=_task_stream_tokens_enabled(runtime.tenant_config),
        )
    )


@app.post("/api/chat")
@require_permission(CHAT_USE)
def api_chat():
    payload = request.get_json(silent=True) or {}
    runtime = get_runtime()
    ui = get_ui_config(runtime.tenant_config)
    context = _payload_context(payload)
    agent = _chat_agent_from_payload(payload, context=context, ui=ui)
    try:
        approval = _chat_approval_from_payload(payload, context=context)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    message = _chat_message_from_payload(payload, context=context)
    if not message and not approval:
        return jsonify({"error": "message is required."}), 400

    chat_service = getattr(runtime, "chat_service", None)
    if chat_service is not None and chat_service.is_answer_agent(agent):
        if approval:
            return jsonify({"error": "context.approval is only valid for action agents."}), 400
        user_id = _effective_user_id(payload, ui)
        conversation_id = _chat_conversation_id(payload, context=context)
        roles, _roles_source = _trusted_business_roles(
            tenant_config=runtime.tenant_config,
            ui=ui,
        )

        try:
            result = chat_service.chat(
                agent=agent,
                user_id=user_id,
                message=message,
                conversation_id=conversation_id,
                roles=roles,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"mode": "answer", **result})

    if agent in _action_agent_names(runtime):
        return _action_chat_response(
            payload=payload,
            runtime=runtime,
            ui=ui,
            agent=agent,
            message=message,
            context=context,
            approval=approval,
        )

    enabled = ", ".join(sorted(row["name"] for row in get_chat_agents(runtime))) or "(none)"
    if chat_service is None:
        return jsonify({"error": "Conversational memory is not available."}), 503
    return jsonify({"error": f"Agent '{agent}' is not enabled. Allowed: {enabled}."}), 400


@app.post("/api/chat/stream")
@require_permission(CHAT_USE)
def api_chat_stream():
    payload = request.get_json(silent=True) or {}
    runtime = get_runtime()
    ui = get_ui_config(runtime.tenant_config)
    context = _payload_context(payload)
    agent = _chat_agent_from_payload(payload, context=context, ui=ui)
    try:
        approval = _chat_approval_from_payload(payload, context=context)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    message = _chat_message_from_payload(payload, context=context)
    if not message and not approval:
        return jsonify({"error": "message is required."}), 400

    chat_service = getattr(runtime, "chat_service", None)
    if chat_service is not None and chat_service.is_answer_agent(agent):
        if approval:
            return jsonify({"error": "context.approval is only valid for action agents."}), 400
        user_id = _effective_user_id(payload, ui)
        conversation_id = _chat_conversation_id(payload, context=context)
        roles, _roles_source = _trusted_business_roles(
            tenant_config=runtime.tenant_config,
            ui=ui,
        )

        def produce_chat() -> dict[str, Any]:
            return {
                "mode": "answer",
                **chat_service.chat(
                    agent=agent,
                    user_id=user_id,
                    message=message,
                    conversation_id=conversation_id,
                    roles=roles,
                ),
            }

        return _sse(stream_response(produce_chat))

    if agent in _action_agent_names(runtime):
        denied = _permission_denied(TASK_APPROVE if approval else TASK_RUN)
        if denied is not None:
            return denied
        try:
            action_runner, conversation_id = _action_chat_runner(
                payload=payload,
                runtime=runtime,
                ui=ui,
                agent=agent,
                message=message,
                context=context,
                approval=approval,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        def produce_action() -> dict[str, Any]:
            return action_runner()

        return _sse(
            stream_response(
                produce_action,
                stream_tokens=_task_stream_tokens_enabled(runtime.tenant_config),
                error_context={"conversation_id": conversation_id} if conversation_id else None,
            )
        )

    enabled = ", ".join(sorted(row["name"] for row in get_chat_agents(runtime))) or "(none)"
    if chat_service is None:
        return jsonify({"error": "Conversational memory is not available."}), 503
    return jsonify({"error": f"Agent '{agent}' is not enabled. Allowed: {enabled}."}), 400


@app.get("/api/conversations")
@require_permission(CHAT_USE)
def api_conversations():
    runtime = get_runtime()
    chat_service = getattr(runtime, "chat_service", None)
    if chat_service is None:
        return jsonify({"conversations": []})
    ui = get_ui_config(runtime.tenant_config)
    agent = str(request.args.get("agent") or ui.get("default_agent") or "")
    user_id = _effective_user_id(request.args, ui)
    return jsonify({"conversations": chat_service.list_conversations(agent=agent, user_id=user_id)})


@app.post("/api/conversations")
@require_permission(CHAT_USE)
def api_create_conversation():
    payload = request.get_json(silent=True) or {}
    runtime = get_runtime()
    chat_service = getattr(runtime, "chat_service", None)
    if chat_service is None:
        return jsonify({"error": "Conversational memory is not available."}), 503
    ui = get_ui_config(runtime.tenant_config)
    agent = str(payload.get("agent") or ui.get("default_agent") or "")
    if not chat_service.is_chat_agent(agent):
        return jsonify({"error": f"Agent '{agent}' is not a configured chat agent."}), 400
    user_id = _effective_user_id(payload, ui)
    title = payload.get("title")
    conversation_id = chat_service.new_conversation(
        agent=agent, user_id=user_id, title=str(title) if title else None
    )
    return jsonify({"conversation_id": conversation_id})


@app.get("/api/conversations/<conversation_id>/messages")
@require_permission(CHAT_USE)
def api_conversation_messages(conversation_id: str):
    runtime = get_runtime()
    chat_service = getattr(runtime, "chat_service", None)
    if chat_service is None:
        return jsonify({"messages": []})
    ui = get_ui_config(runtime.tenant_config)
    user_id = _effective_user_id(request.args, ui)
    return jsonify(
        {"messages": chat_service.messages(conversation_id=conversation_id, user_id=user_id)}
    )


@app.get("/api/runs")
@require_permission(RUNS_VIEW)
def api_runs():
    audit = get_runtime().gateway.audit
    return jsonify({"runs": _safe_runs(audit, limit=50)})


@app.get("/api/runs/<run_id>")
@require_permission(RUNS_VIEW)
def api_run_events(run_id: str):
    audit = get_runtime().gateway.audit
    events = audit.events_for(run_id) if isinstance(audit, SQLiteAuditLog) else []
    return jsonify({"events": events})


@app.get("/api/registry")
@require_permission(GOVERNANCE_VIEW)
def api_registry():
    gateway = get_runtime().gateway
    return jsonify(
        {
            "agents": [agent.__dict__ for agent in gateway.agents.all()],
            "skills": [
                {
                    "name": skill.name,
                    "domain": skill.domain,
                    "description": skill.description,
                    "execution_mode": skill.execution_mode,
                    "permissions": skill.permissions,
                    "tools": skill.tools,
                    "skill_folder": skill.skill_folder,
                    "skill_file": skill.skill_file,
                    "skill_resources": skill.skill_resources,
                }
                for skill in gateway.skills.all()
            ],
            "tools": [
                {
                    "name": tool.name,
                    "domain": tool.domain,
                    "description": tool.description,
                    "supports_batch": tool.supports_batch,
                }
                for tool in gateway.tools.all()
            ],
        }
    )


def format_chat_response(response: dict[str, Any]) -> str:
    final = response.get("output", {}).get("final", {})
    if final.get("message"):
        return str(final["message"])

    ranked = final.get("ranked_candidates", [])
    if not ranked:
        if final.get("campaign_summary"):
            article = final.get("article", {})
            publish = final.get("publish", {})
            quality = final.get("research_quality", {})
            topic_source = final.get("topic_source", "unknown")
            topic_source_label = {
                "request": "用户明确指定",
                "request_keyword": "用户关键词推断",
                "plan_args": "规划参数",
                "tenant_default": "租户默认",
            }.get(str(topic_source), str(topic_source))
            lines = [
                "### 研究结果",
                f"- 选题：{final.get('topic', '未指定')}（来源：{topic_source_label}）",
                (
                    f"- 样本：{quality.get('observed_count', len(final.get('top_cases', [])))}"
                    f"/{quality.get('requested_count', final.get('top_n', 0))}；"
                    f"证据状态：{quality.get('status', 'unknown')}"
                ),
                "- 说明：这是当前搜索页可见样本，不是小红书官方全量日榜。",
                "",
                "### Top 案例",
            ]
            for index, case in enumerate(final.get("top_cases", []), start=1):
                metrics = (
                    f"赞 {case.get('likes', 0)} / 藏 {case.get('saves', 0)} / "
                    f"评 {case.get('comments', 0)}"
                )
                title = str(case.get("title") or "未命名案例")
                url = str(case.get("url") or "")
                title_text = f"[{title}]({url})" if url else title
                lines.append(
                    f"{index}. {title_text}；作者：{case.get('author') or '未知'}；{metrics}"
                )

            lines.extend(["", "### 对比结论"])
            for item in final.get("comparison", []):
                lines.append(
                    f"- **{item.get('pattern', '模式')}**：{item.get('evidence', '')} "
                    f"建议：{item.get('recommendation', '')}"
                )

            warnings = list(quality.get("warnings", []))
            if warnings:
                lines.extend(["", "### 证据与执行限制"])
                lines.extend(f"- {warning}" for warning in warnings)

            lines.extend(
                [
                    "",
                    "### 生成草稿",
                    f"**{article.get('title', '未命名草稿')}**",
                    "",
                    str(article.get("body") or "未生成正文"),
                    "",
                    "### 发布准备",
                    (
                        f"- 状态：{publish.get('status', 'not_prepared')}；"
                        f"readiness：{publish.get('readiness', 'unknown')}；"
                        f"review：{publish.get('review_status', 'unknown')}"
                    ),
                    (
                        f"- 媒体：小红书文字配图；风格：{publish.get('card_style', '未指定')}。"
                        if publish.get("media_strategy") == "xhs_text_image"
                        else "- 媒体：本地图片上传。"
                    ),
                    (
                        "- 当前为 mock 模拟发布，未向真实小红书提交。"
                        if publish.get("provider") == "mock"
                        else (
                            "- 已提交到："
                            + str(publish.get("post_url") or publish.get("channel", "xiaohongshu"))
                            if publish.get("status") == "published"
                            else (
                                "- 当前为模拟/待替换 connector，尚未向真实小红书发布。"
                                if publish.get("requires_real_connector")
                                else f"- 渠道：{publish.get('channel', 'configured channel')}"
                            )
                        )
                    ),
                    "- KPI 是内部运营目标，不构成涨粉结果保证。",
                ]
            )
            return "\n".join(lines)
        message = response.get("output", {}).get("message")
        return str(
            message
            or (
                "The request completed, but no conversational message "
                "or business result was returned."
            )
        )

    lines = []
    if final.get("summary"):
        lines.append(str(final["summary"]))
        lines.append("")
    lines.extend(
        [
            f"Completed. Evaluated {final.get('evaluated_count', len(ranked))} candidates for "
            f"{final.get('job_title', final.get('job_id', 'the selected requisition'))}.",
            "Recommended shortlist:",
        ]
    )
    for index, candidate in enumerate(ranked, start=1):
        matched = ", ".join(candidate.get("matched_skills", [])) or "none"
        lines.append(
            f"{index}. {candidate.get('name')} ({candidate.get('candidate_id')}) - "
            f"score {candidate.get('score')}. Matched: {matched}. {candidate.get('reason')}"
        )
    return "\n".join(lines)


def get_ui_config(tenant_config: dict[str, Any]) -> dict[str, Any]:
    ui = dict(DEFAULT_UI_CONFIG)
    tenant_ui = tenant_config.get("ui", {})
    ui.update(tenant_ui)
    if "chat_agents" not in tenant_ui:
        ui["chat_agents"] = tenant_config.get("chat_agents", ui.get("chat_agents", []))
    return ui


def get_chat_agents(runtime) -> list[dict[str, Any]]:
    config_items = runtime.tenant_config.get("chat_agents", [])
    configured = {str(item.get("name")): item for item in config_items if isinstance(item, dict)}
    enabled_domains = set(runtime.tenant_config.get("enabled_domains", []))
    selected_names = list(configured) or [
        agent.name for agent in runtime.gateway.agents.all() if agent.domain != "platform"
    ]
    rows = []
    for name in selected_names:
        try:
            profile = runtime.gateway.agents.get(name)
        except KeyError:
            continue
        # enabled_domains is the single source of truth for what a tenant exposes:
        # an agent whose domain is not enabled never reaches the console.
        if profile.domain not in enabled_domains:
            continue
        config = configured.get(name, {})
        interaction_mode = str(config.get("mode") or "chat").lower()
        if interaction_mode != "chat":
            interaction_mode = "chat"
        actions_enabled = bool(config.get("actions_enabled", False))
        rows.append(
            {
                "name": profile.name,
                "label": config.get("label") or profile.name.replace("_", " ").title(),
                "domain": profile.domain,
                "mission": config.get("mission") or profile.description,
                "status": config.get("status") or "online",
                "allowed_skills": profile.allowed_skills,
                "allowed_tools": profile.allowed_tools,
                "fields": config.get("fields") or AGENT_CONTEXT_FIELDS.get(name, []),
                "mode": interaction_mode,
                "actions_enabled": actions_enabled,
            }
        )
    return rows


def _action_agent_names(runtime) -> set[str]:
    return {row["name"] for row in get_chat_agents(runtime) if row.get("actions_enabled")}


def _task_stream_tokens_enabled(tenant_config: dict[str, Any]) -> bool:
    policy = str(tenant_config.get("output_review_policy", "warn")).lower()
    return policy not in {"block", "block_on_failed", "fail_closed"}


def display_path(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        display = resolved.relative_to(AGENTKIT_ROOT)
    except ValueError:
        display = resolved
    return str(display).replace("\\", "/")


def format_timestamp(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(value)


def _safe_counts(audit) -> dict[str, int]:
    if isinstance(audit, SQLiteAuditLog):
        return audit.run_counts_by_status()
    return {}


def _safe_runs(audit, *, limit: int) -> list[dict[str, Any]]:
    if isinstance(audit, SQLiteAuditLog):
        return audit.list_runs(limit=limit)
    return []


def _list_or_default(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value]
    return [str(value)]


if __name__ == "__main__":
    # use_reloader stays False on purpose: the Werkzeug reloader spawns child
    # processes that orphan on Windows and keep holding port 8501, which makes
    # the browser hit a stale process. TEMPLATES_AUTO_RELOAD already picks up
    # template/static edits live; only Python changes need a manual restart.
    app.run(host="127.0.0.1", port=8501, debug=False, use_reloader=False)
