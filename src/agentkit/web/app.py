"""AgentKit 统一 Web 控制台与 API。"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from agentkit.config import get_settings
from agentkit.core.audit import SQLiteAuditLog
from agentkit.core.contracts import TaskRequest, TaskResponse
from agentkit.core.identity import (
    CHAT_USE,
    GOVERNANCE_VIEW,
    RUNS_VIEW,
    RUNTIME_ADMIN,
    TASK_APPROVE,
    TASK_RUN,
)
from agentkit.runtime.bootstrap import AGENTKIT_ROOT, build_runtime, resolve_tenant_id
from agentkit.web.identity import current_principal, require_permission
from agentkit.web.security import configure_security
from agentkit.web.streaming import stream_response

DEFAULT_UI_CONFIG = {
    "demo_prompt": "请对 JOB-001 的候选人进行排序。",
    "default_agent": "hr_recruiter",
    "demo_prompts": {},
    "default_user_id": "u-001",
    "default_roles": ["employee"],
}

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
configure_security(app)


@lru_cache(maxsize=8)
def _build_runtime_cached(tenant_id: str):
    return build_runtime(tenant_id=tenant_id)


def get_runtime():
    return _build_runtime_cached(resolve_tenant_id())


def clear_runtime_cache() -> None:
    """清空缓存，使 Agent、Skill、Tool 和租户声明重新编译。"""
    _build_runtime_cached.cache_clear()


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.post("/api/admin/reload")
@require_permission(RUNTIME_ADMIN)
def api_admin_reload():
    from agentkit.core.llm_client import clear_provider_cache

    get_settings.cache_clear()
    clear_provider_cache()
    clear_runtime_cache()
    return jsonify({"status": "reloaded", "tenant_id": resolve_tenant_id()})


@app.get("/api/xhs/publish-assets/<path:filename>")
@require_permission(CHAT_USE)
def api_xhs_publish_asset(filename: str):
    safe_name = Path(filename).name
    if safe_name != filename or not safe_name.lower().endswith((".png", ".jpg", ".jpeg")):
        return jsonify({"error": "非法的发布素材路径"}), 400
    runtime = get_runtime()
    social = runtime.tenant_config.get("social_growth", {})
    configured = social.get("publish_asset_root") if isinstance(social, dict) else None
    root = configured or get_settings().xhs_publish_asset_root
    return send_from_directory(Path(str(root)).expanduser().resolve(), safe_name)


@app.context_processor
def inject_global_context() -> dict[str, Any]:
    runtime = get_runtime()
    return {
        "tenant_id": runtime.tenant_config["tenant_id"],
        "db_path": display_path(runtime.db_path),
        "demo_prompt": get_ui_config(runtime.tenant_config)["demo_prompt"],
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
    counts = _safe_counts(gateway.audit)
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    resolved = completed + failed
    metrics = [
        {"label": "Runs Completed", "value": completed, "helper": "Durable audit"},
        {
            "label": "Success Rate",
            "value": f"{round(completed / resolved * 100, 1) if resolved else 100.0}%",
            "helper": "Latest environment",
        },
        {
            "label": "Active Runs",
            "value": counts.get("running", 0),
            "helper": "Currently executing",
        },
        {
            "label": "Agents Online",
            "value": len(gateway.agents.all()),
            "helper": "Explicit business agents",
        },
        {
            "label": "Skills Online",
            "value": len(gateway.skills.all()),
            "helper": "Declarative capabilities",
        },
    ]
    agent_rows = [
        {
            "Agent": agent.name,
            "Domain": agent.domain,
            "Health": "Ready",
            "Skills": len(agent.allowed_skills),
        }
        for agent in gateway.agents.all()
    ]
    return render_template(
        "overview.html",
        active="overview",
        title="Management Dashboard",
        metrics=metrics,
        impact_rows=[],
        agent_rows=agent_rows,
        runs=_safe_runs(gateway.audit, limit=8),
        capabilities=[
            {"name": name, "status": "Live", "description": "Unified execution strategy"}
            for name in runtime.strategy_names
        ],
    )


@app.get("/chat")
def chat_console():
    runtime = get_runtime()
    return render_template(
        "chat.html",
        active="chat",
        title="Chat Console",
        ui=get_ui_config(runtime.tenant_config),
        agents=get_agent_cards(runtime),
    )


@app.get("/operations")
def operations():
    runtime = get_runtime()
    audit = runtime.gateway.audit
    runs = _safe_runs(audit, limit=50)
    selected_run_id = request.args.get("run_id") or (runs[0]["run_id"] if runs else "")
    events = audit.events_for(selected_run_id) if selected_run_id else []
    counts = _safe_counts(audit)
    event_rows = [
        {
            "Time": format_timestamp(event["ts"]),
            "Event": event["type"],
            "Details": json.dumps(event["payload"], ensure_ascii=False),
        }
        for event in events
    ]
    metrics = [
        {"label": label, "value": counts.get(status, 0), "helper": "Audit status"}
        for label, status in (
            ("Completed", "completed"),
            ("Running", "running"),
            ("Waiting", "waiting_for_approval"),
            ("Failed", "failed"),
        )
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
    agents = [
        {
            "Name": agent.name,
            "Domain": agent.domain,
            "Model": agent.model,
            "Allowed Skills": ", ".join(agent.allowed_skills),
            "Description": agent.description,
        }
        for agent in gateway.agents.all()
    ]
    skills = [
        {
            "Name": skill.name,
            "Domain": skill.domain,
            "Mode": (
                f"{skill.execution.reasoning.value}/"
                f"{skill.execution.orchestration.value}/"
                f"{skill.execution.tool_policy.value}"
            ),
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
    contexts = [
        {
            "ID": item["id"],
            "Version": item["version"],
            "Hash": item["hash"],
            "Override Hash": item["override_hash"] or "-",
            "Max Input Tokens": item["max_input_tokens"],
        }
        for item in runtime.contexts.manifest()
    ]
    audit = gateway.audit
    return render_template(
        "governance.html",
        active="governance",
        title="Governance",
        agents=agents,
        skills=skills,
        tools=tools,
        contexts=contexts,
        event_counts=audit.event_counts_by_type() if isinstance(audit, SQLiteAuditLog) else [],
        cost_summary=audit.cost_summary() if isinstance(audit, SQLiteAuditLog) else {},
    )


def _effective_user_id(payload: dict[str, Any], ui: dict[str, Any]) -> str:
    principal = current_principal()
    return principal.subject if principal.is_authenticated else str(
        payload.get("user_id") or ui["default_user_id"]
    )


def _trusted_business_roles(
    *,
    tenant_config: dict[str, Any],
    ui: dict[str, Any],
) -> tuple[list[str], str]:
    """业务角色只来自可信身份或租户映射，不接受请求体提权。"""
    principal = current_principal()
    permission_map = tenant_config.get("role_permissions", {})
    known = set(permission_map) if isinstance(permission_map, dict) else set()
    claimed = [
        role
        for role in _as_list(principal.claims.get("business_roles"))
        if role in known
    ]
    if claimed:
        return claimed, "principal.claims.business_roles"
    mapping = tenant_config.get("principal_business_roles", {})
    mapped: list[str] = []
    if isinstance(mapping, dict):
        for principal_role in principal.roles:
            for role in _as_list(mapping.get(principal_role)):
                if role in known and role not in mapped:
                    mapped.append(role)
    if mapped:
        return mapped, "tenant.principal_business_roles"
    overlap = [role for role in principal.roles if role in known]
    if overlap:
        return overlap, "principal.roles"
    if principal.auth_method == "proxy":
        return [], "none"
    fallback = [role for role in _as_list(ui.get("default_roles")) if role in known]
    return fallback, "tenant.ui.default_roles"


def _extract_payload(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("context")
    merged = dict(nested) if isinstance(nested, dict) else {}
    for key, value in payload.items():
        if key != "context" and key not in merged:
            merged[key] = value
    if "text" not in merged and "message" in merged:
        merged["text"] = merged["message"]
    return merged


def _task_request(payload: dict[str, Any]) -> TaskRequest:
    runtime = get_runtime()
    ui = get_ui_config(runtime.tenant_config)
    values = _extract_payload(payload)
    agent_id = str(values.get("agent") or ui.get("default_agent") or "")
    if agent_id not in {agent.name for agent in runtime.gateway.agents.all()}:
        raise ValueError(f"未启用的 Agent: {agent_id or '<empty>'}")
    text = str(values.get("text") or ui["demo_prompt"])
    roles, role_source = _trusted_business_roles(
        tenant_config=runtime.tenant_config,
        ui=ui,
    )
    reserved = {
        "agent",
        "approval",
        "approved_skills",
        "context",
        "message",
        "rejected_skills",
        "roles",
        "text",
        "user_id",
    }
    context = {key: value for key, value in values.items() if key not in reserved}
    context.update(
        {
            "agent": agent_id,
            "principal": current_principal().to_public_dict(),
            "business_roles_source": role_source,
        }
    )
    if "roles" in values:
        context["ignored_payload_roles"] = _as_list(values["roles"])
    return TaskRequest(
        user_id=_effective_user_id(values, ui),
        roles=roles,
        text=text,
        context=context,
    )


def _approval(payload: dict[str, Any]) -> tuple[str, list[str], list[str]] | None:
    values = _extract_payload(payload)
    raw = values.get("approval")
    if not isinstance(raw, dict):
        return None
    thread_id = str(raw.get("thread_id") or "")
    if not thread_id:
        raise ValueError("审批恢复缺少 thread_id")
    skills = _as_list(raw.get("skills"))
    action = str(raw.get("action") or "approve").lower()
    return thread_id, ([] if action == "reject" else skills), (skills if action == "reject" else [])


def _resume_payload(payload: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    thread_id = str(payload.get("thread_id") or "")
    if not thread_id:
        raise ValueError("审批恢复缺少 thread_id")
    approved = _as_list(payload.get("approved_skills"))
    rejected = _as_list(payload.get("rejected_skills"))
    if not approved and not rejected:
        raise ValueError("必须提供 approved_skills 或 rejected_skills")
    overlap = sorted(set(approved) & set(rejected))
    if overlap:
        raise ValueError("同一 Skill 不能同时批准和拒绝: " + ", ".join(overlap))
    return thread_id, approved, rejected


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = get_runtime()
    approval = _approval(payload)
    if approval is not None:
        thread_id, approved, rejected = approval
        response = runtime.gateway.resume(
            thread_id,
            approved_skills=approved,
            rejected_skills=rejected,
            decision_context={"principal": current_principal().to_public_dict()},
        )
    else:
        response = runtime.gateway.handle(_task_request(payload))
    return _web_response(response)


def _resume(payload: dict[str, Any]) -> dict[str, Any]:
    thread_id, approved, rejected = _resume_payload(payload)
    response = get_runtime().gateway.resume(
        thread_id,
        approved_skills=approved,
        rejected_skills=rejected,
        decision_context={"principal": current_principal().to_public_dict()},
    )
    return _web_response(response)


def _web_response(response: TaskResponse) -> dict[str, Any]:
    body = response.to_dict()
    return {
        "interaction_mode": "unified",
        "agent": response.agent,
        "strategy": response.strategy,
        "conversation_id": response.conversation_id,
        "run_id": response.run_id,
        "assistant_text": format_response_text(response),
        "response": body,
    }


def format_response_text(response: TaskResponse) -> str:
    output = response.output
    for key in ("answer", "message", "summary", "campaign_summary"):
        if output.get(key):
            return str(output[key])
    if response.status == "waiting_for_approval":
        skills = ", ".join(output.get("approval", {}).get("skills", []))
        return f"当前任务等待人工审批: {skills}"
    if response.status == "needs_clarification":
        missing = ", ".join(output.get("missing_required", []))
        return f"请补充必填参数: {missing}"
    ranked = output.get("ranked_candidates")
    if isinstance(ranked, list):
        return "\n".join(
            f"{index}. {item.get('name', item.get('candidate_id', 'candidate'))}"
            for index, item in enumerate(ranked, start=1)
        )
    return json.dumps(output, ensure_ascii=False, default=str)


def _sse(produce, *, error_context: dict[str, Any] | None = None) -> Response:
    response = Response(
        stream_response(produce, error_context=error_context),
        mimetype="text/event-stream",
    )
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


@app.post("/api/chat")
@require_permission(CHAT_USE)
def api_chat():
    try:
        return jsonify(_run(request.get_json(silent=True) or {}))
    except (KeyError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/chat/stream")
@require_permission(CHAT_USE)
def api_chat_stream():
    payload = request.get_json(silent=True) or {}
    return _sse(lambda: _run(payload))


@app.post("/api/tasks")
@require_permission(TASK_RUN)
def api_tasks():
    try:
        return jsonify(_run(request.get_json(silent=True) or {}))
    except (KeyError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/tasks/stream")
@require_permission(TASK_RUN)
def api_tasks_stream():
    payload = request.get_json(silent=True) or {}
    return _sse(lambda: _run(payload))


@app.post("/api/tasks/resume")
@app.post("/api/tasks/approve")
@require_permission(TASK_APPROVE)
def api_tasks_resume():
    try:
        return jsonify(_resume(request.get_json(silent=True) or {}))
    except (KeyError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/tasks/resume/stream")
@app.post("/api/tasks/approve/stream")
@require_permission(TASK_APPROVE)
def api_tasks_resume_stream():
    payload = request.get_json(silent=True) or {}
    return _sse(lambda: _resume(payload))


@app.get("/api/conversations")
@require_permission(CHAT_USE)
def api_conversations():
    runtime = get_runtime()
    agent = str(request.args.get("agent") or get_ui_config(runtime.tenant_config)["default_agent"])
    rows = runtime.conversations.list_conversations(
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        agent=agent,
        user_id=_effective_user_id({}, get_ui_config(runtime.tenant_config)),
    )
    return jsonify({"conversations": rows})


@app.post("/api/conversations")
@require_permission(CHAT_USE)
def api_create_conversation():
    runtime = get_runtime()
    payload = request.get_json(silent=True) or {}
    agent = str(payload.get("agent") or get_ui_config(runtime.tenant_config)["default_agent"])
    conversation_id = runtime.conversations.create_conversation(
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        agent=agent,
        user_id=_effective_user_id(payload, get_ui_config(runtime.tenant_config)),
        title=str(payload.get("title") or "New conversation"),
    )
    return jsonify({"conversation_id": conversation_id}), 201


@app.get("/api/conversations/<conversation_id>/messages")
@require_permission(CHAT_USE)
def api_conversation_messages(conversation_id: str):
    runtime = get_runtime()
    conversation = runtime.conversations.get_conversation(conversation_id)
    user_id = _effective_user_id({}, get_ui_config(runtime.tenant_config))
    if conversation is None or conversation.get("user_id") != user_id:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"messages": runtime.conversations.all_messages(conversation_id)})


@app.get("/api/runs")
@require_permission(RUNS_VIEW)
def api_runs():
    return jsonify({"runs": _safe_runs(get_runtime().gateway.audit, limit=50)})


@app.get("/api/runs/<run_id>")
@require_permission(RUNS_VIEW)
def api_run_events(run_id: str):
    return jsonify({"events": get_runtime().gateway.audit.events_for(run_id)})


@app.get("/api/registry")
@require_permission(GOVERNANCE_VIEW)
def api_registry():
    runtime = get_runtime()
    gateway = runtime.gateway
    return jsonify(
        {
            "agents": [
                {
                    "name": agent.name,
                    "domain": agent.domain,
                    "description": agent.description,
                    "skills": agent.allowed_skills,
                    "allowed_strategies": [
                        item.value for item in agent.execution_policy.allowed_strategies
                    ],
                }
                for agent in gateway.agents.all()
            ],
            "skills": [
                {
                    "name": skill.name,
                    "domain": skill.domain,
                    "description": skill.description,
                    "reasoning": skill.execution.reasoning.value,
                    "orchestration": skill.execution.orchestration.value,
                    "tool_policy": skill.execution.tool_policy.value,
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
                    "provider": tool.provider.value,
                    "risk": tool.risk.value,
                    "supports_batch": tool.supports_batch,
                }
                for tool in gateway.tools.all()
            ],
            "strategies": list(runtime.strategy_names),
        }
    )


def get_ui_config(tenant_config: dict[str, Any]) -> dict[str, Any]:
    ui = dict(DEFAULT_UI_CONFIG)
    raw = tenant_config.get("ui", {})
    if isinstance(raw, dict):
        ui.update(raw)
    return ui


def get_agent_cards(runtime) -> list[dict[str, Any]]:
    rows = []
    for profile in runtime.gateway.agents.all():
        tools = sorted(
            {
                tool
                for skill_name in profile.allowed_skills
                for tool in runtime.gateway.skills.get(skill_name).tools
            }
        )
        rows.append(
            {
                "name": profile.name,
                "label": profile.name.replace("_", " ").title(),
                "domain": profile.domain,
                "mission": profile.description,
                "status": "online",
                "allowed_skills": profile.allowed_skills,
                "allowed_tools": tools,
                "fields": [],
            }
        )
    return rows


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


def _safe_counts(audit: Any) -> dict[str, int]:
    return audit.run_counts_by_status() if isinstance(audit, SQLiteAuditLog) else {}


def _safe_runs(audit: Any, *, limit: int) -> list[dict[str, Any]]:
    return audit.list_runs(limit=limit) if isinstance(audit, SQLiteAuditLog) else []


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value]
    return [str(value)]


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8501, debug=False, use_reloader=False)
