"""AgentKit 统一 Web 控制台与 API。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_from_directory,
)

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
    Principal,
)
from agentkit.core.response_text import (
    format_task_output_text,
    normalize_persisted_assistant_text,
)
from agentkit.runtime.bootstrap import (
    AGENTKIT_ROOT,
    AgentKitRuntime,
    build_runtime,
    resolve_tenant_id,
)
from agentkit.runtime.conversation_deletion import (
    ConversationBusyError,
    ConversationNotFoundError,
)
from agentkit.web.identity import current_principal, require_permission
from agentkit.web.security import configure_security
from agentkit.web.streaming import stream_response

DEFAULT_UI_CONFIG = {
    "demo_prompt": "请对 JOB-001 的候选人进行排序。",
    "default_agent": "general_agent",
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


@app.template_filter("datetime_ts")
def datetime_ts_filter(value: Any) -> str:
    """Return a machine-readable timestamp for HTML ``datetime`` attributes."""
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(float(value)).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return str(value)


@app.get("/")
def overview():
    return chat_console()


@app.get("/overview")
def management_overview():
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


@app.get("/agents")
def agent_network():
    return render_template(
        "agents.html",
        active="agents",
        title="Agent Network",
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
    selected_run = next((run for run in runs if run["run_id"] == selected_run_id), None)
    events = (
        audit.events_for(selected_run_id)
        if isinstance(audit, SQLiteAuditLog) and selected_run_id
        else []
    )
    child_runs = (
        audit.child_runs(selected_run_id)
        if hasattr(audit, "child_runs") and selected_run_id
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
            "timestamp": event["ts"],
            "time": format_timestamp(event["ts"]),
            "type": event["type"],
            "payload": event.get("payload", {}),
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
        selected_run=selected_run,
        event_rows=event_rows,
        child_runs=child_runs,
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


def _effective_user_id(
    payload: dict[str, Any],
    ui: dict[str, Any],
    *,
    principal: Principal | None = None,
) -> str:
    principal = principal if principal is not None else current_principal()
    return (
        principal.subject
        if principal.is_authenticated
        else str(payload.get("user_id") or ui["default_user_id"])
    )


def _trusted_business_roles(
    *,
    tenant_config: dict[str, Any],
    ui: dict[str, Any],
    principal: Principal | None = None,
) -> tuple[list[str], str]:
    """业务角色只来自可信身份或租户映射，不接受请求体提权。"""
    principal = principal if principal is not None else current_principal()
    permission_map = tenant_config.get("role_permissions", {})
    known = set(permission_map) if isinstance(permission_map, dict) else set()
    claimed = [role for role in _as_list(principal.claims.get("business_roles")) if role in known]
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


def _task_request(
    payload: dict[str, Any],
    *,
    runtime: AgentKitRuntime | None = None,
    principal: Principal | None = None,
) -> TaskRequest:
    runtime = runtime if runtime is not None else get_runtime()
    principal = principal if principal is not None else current_principal()
    ui = get_ui_config(runtime.tenant_config)
    values = _extract_payload(payload)
    agent_id = str(values.get("agent") or ui.get("default_agent") or "")
    if agent_id not in {agent.name for agent in runtime.gateway.agents.all()}:
        raise ValueError(f"未启用的 Agent: {agent_id or '<empty>'}")
    text = str(values.get("text") or ui["demo_prompt"])
    roles, role_source = _trusted_business_roles(
        tenant_config=runtime.tenant_config,
        ui=ui,
        principal=principal,
    )
    reserved = {
        "agent",
        "approval",
        "approved_skills",
        "context",
        "message",
        "rejected_skills",
        "retry_of_run_id",
        "roles",
        "text",
        "user_id",
    }
    context = {key: value for key, value in values.items() if key not in reserved}
    context.update(
        {
            "agent": agent_id,
            "principal": principal.to_public_dict(),
            "business_roles_source": role_source,
        }
    )
    if "roles" in values:
        context["ignored_payload_roles"] = _as_list(values["roles"])
    return TaskRequest(
        user_id=_effective_user_id(values, ui, principal=principal),
        roles=roles,
        text=text,
        context=context,
    )


def _chat_task_request(
    payload: dict[str, Any],
    *,
    runtime: AgentKitRuntime | None = None,
    principal: Principal | None = None,
) -> TaskRequest:
    """聊天入口始终以 General Agent 为会话所有者。"""
    normalized = dict(payload)
    nested = normalized.get("context")
    normalized["context"] = {
        **(dict(nested) if isinstance(nested, dict) else {}),
        "agent": "general_agent",
    }
    normalized["agent"] = "general_agent"
    task = _task_request(normalized, runtime=runtime, principal=principal)
    return replace(task, context={**task.context, "agent": "general_agent"})


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


def _run_chat(
    payload: dict[str, Any],
    *,
    runtime: AgentKitRuntime | None = None,
    principal: Principal | None = None,
    trusted_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = runtime if runtime is not None else get_runtime()
    principal = principal if principal is not None else current_principal()
    if runtime.chat_service is None:
        raise RuntimeError("多 Agent 聊天服务未初始化")
    task = _chat_task_request(payload, runtime=runtime, principal=principal)
    if trusted_context:
        task = replace(task, context={**task.context, **trusted_context})
    conversation_id = str(task.context.get("conversation_id") or "")
    if conversation_id:
        conversation = runtime.conversations.get_conversation(conversation_id)
        if (
            conversation is None
            or conversation.get("tenant_id") != str(runtime.tenant_config["tenant_id"])
            or conversation.get("agent") != "general_agent"
            or conversation.get("user_id") != task.user_id
        ):
            raise ValueError("会话不存在或无权访问")
        if conversation.get("status") != "active":
            raise ValueError("会话正在删除，不能继续执行")
    approval = _approval(payload)
    if approval is not None:
        thread_id, approved, rejected = approval
        response = runtime.chat_service.resume(
            thread_id,
            user_id=task.user_id,
            roles=task.roles,
            approved_skills=approved,
            rejected_skills=rejected,
            decision_context={"principal": principal.to_public_dict()},
        )
    else:
        response = runtime.chat_service.handle(task)
    return _web_response(response)


def _run_task(
    payload: dict[str, Any],
    *,
    runtime: AgentKitRuntime | None = None,
    principal: Principal | None = None,
) -> dict[str, Any]:
    runtime = runtime if runtime is not None else get_runtime()
    principal = principal if principal is not None else current_principal()
    task = _task_request(payload, runtime=runtime, principal=principal)
    return _web_response(runtime.gateway.handle(task))


def _resume(
    payload: dict[str, Any],
    *,
    runtime: AgentKitRuntime | None = None,
    principal: Principal | None = None,
) -> dict[str, Any]:
    runtime = runtime if runtime is not None else get_runtime()
    principal = principal if principal is not None else current_principal()
    thread_id, approved, rejected = _resume_payload(payload)
    response = runtime.gateway.resume(
        thread_id,
        approved_skills=approved,
        rejected_skills=rejected,
        decision_context={"principal": principal.to_public_dict()},
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
    return format_task_output_text(status=response.status, output=response.output)


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
        return jsonify(_run_chat(request.get_json(silent=True) or {}))
    except (KeyError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/chat/stream")
@require_permission(CHAT_USE)
def api_chat_stream():
    payload = request.get_json(silent=True) or {}
    runtime = get_runtime()
    principal = current_principal()
    return _sse(lambda: _run_chat(payload, runtime=runtime, principal=principal))


@app.post("/api/tasks")
@require_permission(TASK_RUN)
def api_tasks():
    try:
        return jsonify(_run_task(request.get_json(silent=True) or {}))
    except (KeyError, RuntimeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/tasks/stream")
@require_permission(TASK_RUN)
def api_tasks_stream():
    payload = request.get_json(silent=True) or {}
    runtime = get_runtime()
    principal = current_principal()
    return _sse(lambda: _run_task(payload, runtime=runtime, principal=principal))


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
    runtime = get_runtime()
    principal = current_principal()
    return _sse(lambda: _resume(payload, runtime=runtime, principal=principal))


@app.get("/api/conversations")
@require_permission(CHAT_USE)
def api_conversations():
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    user_id = _effective_user_id({}, get_ui_config(runtime.tenant_config))
    rows = runtime.conversations.list_conversations(
        tenant_id=tenant_id,
        agent="general_agent",
        user_id=user_id,
    )
    return jsonify({"conversations": rows})


@app.post("/api/conversations")
@require_permission(CHAT_USE)
def api_create_conversation():
    runtime = get_runtime()
    payload = request.get_json(silent=True) or {}
    conversation_id = runtime.conversations.create_conversation(
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        agent="general_agent",
        user_id=_effective_user_id(payload, get_ui_config(runtime.tenant_config)),
        title=str(payload.get("title") or "New conversation"),
    )
    return jsonify({"conversation_id": conversation_id}), 201


@app.delete("/api/conversations/<conversation_id>")
@require_permission(CHAT_USE)
def api_delete_conversation(conversation_id: str):
    runtime = get_runtime()
    user_id = _effective_user_id({}, get_ui_config(runtime.tenant_config))
    try:
        result = runtime.conversation_deletion.delete(
            conversation_id=conversation_id,
            tenant_id=str(runtime.tenant_config["tenant_id"]),
            user_id=user_id,
            agent="general_agent",
        )
    except ConversationNotFoundError:
        return jsonify({"error": "会话不存在"}), 404
    except ConversationBusyError:
        return jsonify({"error": "该会话仍有任务正在执行或等待审批，请先结束任务"}), 409
    except Exception:  # noqa: BLE001 - API 边界隐藏存储与向量后端内部细节
        app.logger.exception(
            "conversation deletion failed",
            extra={"conversation_id": conversation_id},
        )
        return jsonify({"error": "会话删除失败，请稍后重试"}), 503
    return jsonify({"status": "deleted", "conversation_id": result.conversation_id})


@app.post("/api/conversations/<conversation_id>/terminate-and-delete")
@require_permission(CHAT_USE)
def api_terminate_and_delete_conversation(conversation_id: str):
    runtime = get_runtime()
    user_id = _effective_user_id({}, get_ui_config(runtime.tenant_config))
    try:
        result = runtime.conversation_deletion.terminate_and_delete(
            conversation_id=conversation_id,
            tenant_id=str(runtime.tenant_config["tenant_id"]),
            user_id=user_id,
            agent="general_agent",
        )
    except ConversationNotFoundError:
        return jsonify({"error": "会话不存在"}), 404
    except ConversationBusyError:
        return jsonify({"error": "任务正在运行，请等待完成后再删除"}), 409
    except Exception:  # noqa: BLE001 - API 边界隐藏存储与取消内部细节
        app.logger.exception(
            "conversation termination failed",
            extra={"conversation_id": conversation_id},
        )
        return jsonify({"error": "结束任务失败，请稍后重试"}), 503
    return jsonify(
        {
            "status": result.status,
            "conversation_id": result.conversation_id,
        }
    )


@app.post("/api/conversations/<conversation_id>/retry/stream")
@require_permission(CHAT_USE)
def api_retry_conversation_stream(conversation_id: str):
    runtime = get_runtime()
    principal = current_principal()
    user_id = _effective_user_id({}, get_ui_config(runtime.tenant_config))
    conversation = runtime.conversations.get_conversation(conversation_id)
    if (
        conversation is None
        or conversation.get("tenant_id") != str(runtime.tenant_config["tenant_id"])
        or conversation.get("agent") != "general_agent"
        or conversation.get("user_id") != user_id
    ):
        return jsonify({"error": "会话不存在"}), 404
    if conversation.get("status") != "active":
        return jsonify({"error": "该会话正在删除，不能重新执行"}), 409
    execution = runtime.conversation_runs.resolve(
        conversation_id=conversation_id,
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        user_id=user_id,
    )
    if not execution.retryable or not execution.original_request:
        return jsonify({"error": "该会话当前不能重新执行"}), 409
    payload = {
        "message": execution.original_request,
        "context": {"conversation_id": conversation_id},
    }
    return _sse(
        lambda: _run_chat(
            payload,
            runtime=runtime,
            principal=principal,
            trusted_context={"retry_of_run_id": execution.latest_run_id},
        )
    )


@app.get("/api/conversations/<conversation_id>/messages")
@require_permission(CHAT_USE)
def api_conversation_messages(conversation_id: str):
    runtime = get_runtime()
    conversation = runtime.conversations.get_conversation(conversation_id)
    user_id = _effective_user_id({}, get_ui_config(runtime.tenant_config))
    if (
        conversation is None
        or conversation.get("tenant_id") != str(runtime.tenant_config["tenant_id"])
        or conversation.get("agent") != "general_agent"
        or conversation.get("user_id") != user_id
    ):
        return jsonify({"error": "会话不存在"}), 404
    rows = runtime.conversations.all_messages(conversation_id)
    execution = runtime.conversation_runs.resolve(
        conversation_id=conversation_id,
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        user_id=user_id,
    )
    return jsonify(
        {
            "messages": _display_conversation_messages(rows),
            "execution": execution.to_dict(),
        }
    )


def _display_conversation_messages(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    displayed: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("role") == "assistant":
            item["content"] = normalize_persisted_assistant_text(str(item.get("content") or ""))
        displayed.append(item)
    return displayed


@app.get("/api/runs")
@require_permission(RUNS_VIEW)
def api_runs():
    return jsonify({"runs": _safe_runs(get_runtime().gateway.audit, limit=50)})


@app.get("/api/runs/<run_id>")
@require_permission(RUNS_VIEW)
def api_run_events(run_id: str):
    audit = get_runtime().gateway.audit
    return jsonify(
        {
            "events": audit.events_for(run_id),
            "children": audit.child_runs(run_id) if hasattr(audit, "child_runs") else [],
        }
    )


@app.get("/api/registry")
@require_permission(GOVERNANCE_VIEW)
def api_registry():
    runtime = get_runtime()
    gateway = runtime.gateway
    directory = dict(runtime.tenant_config.get("agent_directory") or {})
    return jsonify(
        {
            "agents": [
                {
                    "name": agent.name,
                    "label": str(
                        (directory.get(agent.name) or {}).get("label")
                        or agent.name.replace("_", " ").title()
                    ),
                    "aliases": list((directory.get(agent.name) or {}).get("aliases") or []),
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
            "relationships": [
                {
                    "source": "general_agent",
                    "target": agent.name,
                    "type": "coordinates",
                }
                for agent in gateway.agents.all()
                if agent.name != "general_agent"
            ]
            + [
                {"source": agent.name, "target": skill, "type": "binds"}
                for agent in gateway.agents.all()
                for skill in agent.allowed_skills
            ]
            + [
                {"source": skill.name, "target": tool, "type": "uses"}
                for skill in gateway.skills.all()
                for tool in skill.tools
            ],
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
    directory = dict(runtime.tenant_config.get("agent_directory") or {})
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
                "label": str(
                    (directory.get(profile.name) or {}).get("label")
                    or profile.name.replace("_", " ").title()
                ),
                "aliases": list((directory.get(profile.name) or {}).get("aliases") or []),
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
