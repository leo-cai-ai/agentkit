"""统一 Chat API 集成测试。"""

from __future__ import annotations

import json
import re

import pytest

import agentkit.config as config_mod


def _responder(system: str, user: str) -> str:
    if "可选动作" in system and "delegate" in system:
        return json.dumps(
            {
                "action": "delegate",
                "target_agent": "customer_service",
                "task": "回答客服问题",
                "reason": "属于客服能力",
                "confidence": "high",
            }
        )
    if "General Agent" in system and "直接回答" in system:
        return "我是 General Agent，可以继续帮助你。"
    if "意图分解节点" in system:
        return json.dumps(
            {
                "intent_type": "business_task",
                "goal": "回答客服问题",
                "target": {"kind": "business_skill", "name": "customer.answer"},
                "entities": {},
                "confidence": "high",
                "signals": [],
            }
        )
    return "{}"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    config_mod.get_settings.cache_clear()

    import agentkit.core.llm_client as llm_client
    import agentkit.runtime.bootstrap as bootstrap_mod
    from agentkit.llm.fake import FakeProvider
    from agentkit.web.app import app, clear_runtime_cache
    from agentkit.web.security import configure_security

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_responder))
    monkeypatch.setattr(bootstrap_mod, "DATA_DIR", tmp_path)
    configure_security(app)
    clear_runtime_cache()
    yield app.test_client()
    clear_runtime_cache()
    config_mod.get_settings.cache_clear()


def _login(client) -> str:
    assert client.post("/login", data={"token": "secret-token"}).status_code == 302
    page = client.get("/chat")
    return re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()


def _sse_final(response) -> dict:
    frames = response.get_data(as_text=True).split("\n\n")
    for frame in frames:
        if not frame.startswith("event: final\n"):
            continue
        data = next(
            line[6:] for line in frame.splitlines() if line.startswith("data: ")
        )
        return json.loads(data)
    raise AssertionError("SSE response did not contain a final event")


def test_chat_api_always_returns_unified_gateway_contract(client) -> None:
    token = _login(client)
    response = client.post(
        "/api/chat",
        json={
            "agent": "customer_service",
            "message": "退货期限是多久？",
            "skill": "customer.answer",
        },
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["interaction_mode"] == "unified"
    assert data["agent"] == "customer_service"
    assert data["strategy"] == "direct"
    assert data["response"]["status"] == "completed"
    assert data["conversation_id"]


def test_chat_stream_captures_request_identity_before_worker_thread(client) -> None:
    token = _login(client)
    response = client.post(
        "/api/chat/stream",
        json={"message": "你好"},
        headers={"X-CSRF-Token": token},
        buffered=True,
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Working outside of application context" not in body
    assert "event: error" not in body
    assert "event: final" in body
    assert '"interaction_mode": "unified"' in body


def test_task_stream_captures_request_identity_before_worker_thread(client) -> None:
    token = _login(client)
    response = client.post(
        "/api/tasks/stream",
        json={
            "agent": "customer_service",
            "text": "请帮助我",
            "skill": "customer.answer",
        },
        headers={"X-CSRF-Token": token},
        buffered=True,
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Working outside of application context" not in body
    assert "event: error" not in body
    assert "event: final" in body


def test_chat_ignores_legacy_agent_selection_and_uses_general_entry(client) -> None:
    token = _login(client)
    response = client.post(
        "/api/chat",
        json={
            "agent": "does_not_exist",
            "context": {"agent": "also_invalid"},
            "message": "请帮我处理售后",
        },
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 200
    assert response.get_json()["response"]["governance"]["route"]["type"] == "general_delegate"


def test_chat_and_task_endpoints_share_response_shape(client) -> None:
    token = _login(client)
    payload = {"agent": "customer_service", "text": "请帮助我", "skill": "customer.answer"}
    chat = client.post("/api/chat", json=payload, headers={"X-CSRF-Token": token}).get_json()
    task = client.post("/api/tasks", json=payload, headers={"X-CSRF-Token": token}).get_json()
    assert set(chat) == set(task)
    assert chat["interaction_mode"] == task["interaction_mode"] == "unified"


def test_conversation_endpoints_use_unified_persistence(client) -> None:
    token = _login(client)
    result = client.post(
        "/api/chat",
        json={"agent": "customer_service", "message": "请帮助我", "skill": "customer.answer"},
        headers={"X-CSRF-Token": token},
    ).get_json()
    conversation_id = result["conversation_id"]
    rows = client.get("/api/conversations?agent=customer_service").get_json()["conversations"]
    messages = client.get(f"/api/conversations/{conversation_id}/messages").get_json()["messages"]
    assert any(row["id"] == conversation_id for row in rows)
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[-1]["agent_id"] == "customer_service"


def test_history_api_normalizes_legacy_structured_assistant_message(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    created = client.post(
        "/api/conversations",
        json={"title": "旧会话"},
        headers={"X-CSRF-Token": token},
    )
    conversation_id = created.get_json()["conversation_id"]
    runtime = get_runtime()
    runtime.conversations.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content=json.dumps(
            {
                "campaign_id": "XHS-30D-10000",
                "platform": "xiaohongshu",
                "topic": "AI时代的副业",
                "workflow_status": "blocked",
                "workflow_trace": [{"step": "xhs.trend.research"}],
                "publish": {
                    "status": "blocked",
                    "review": {"reason": "证据不足"},
                },
            },
            ensure_ascii=False,
        ),
        agent_id="xhs_growth",
    )

    response = client.get(f"/api/conversations/{conversation_id}/messages")
    assistant = response.get_json()["messages"][-1]

    assert response.status_code == 200
    assert assistant["content"] == "内容审核未通过，未进入发布：证据不足"
    assert "workflow_trace" not in assistant["content"]


def test_delete_conversation_removes_history_but_keeps_run_trace(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    result = client.post(
        "/api/chat",
        json={"message": "你好"},
        headers={"X-CSRF-Token": token},
    ).get_json()
    conversation_id = result["conversation_id"]
    run_id = result["run_id"]

    response = client.delete(
        f"/api/conversations/{conversation_id}",
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "deleted",
        "conversation_id": conversation_id,
    }
    runtime = get_runtime()
    assert runtime.conversations.get_conversation(conversation_id) is None
    assert runtime.gateway.audit.get_run(run_id) is not None


@pytest.mark.parametrize("status", ["running", "waiting_for_approval"])
def test_delete_conversation_rejects_blocking_run(client, status) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    conversation_id = runtime.conversations.create_conversation(
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        agent="general_agent",
        user_id="console-admin",
    )
    run_id = runtime.gateway.audit.start_run(
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        user_id="console-admin",
        text="处理中",
        conversation_id=conversation_id,
    )
    if status == "waiting_for_approval":
        runtime.gateway.audit.record(
            run_id,
            "run_paused",
            {"status": "waiting_for_approval"},
        )

    response = client.delete(
        f"/api/conversations/{conversation_id}",
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 409
    assert "正在执行或等待审批" in response.get_json()["error"]
    assert runtime.conversations.get_conversation(conversation_id) is not None


@pytest.mark.parametrize(
    ("agent", "user_id"),
    [("other_agent", "console-admin"), ("general_agent", "other-user")],
)
def test_delete_conversation_hides_foreign_scope(client, agent, user_id) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    conversation_id = runtime.conversations.create_conversation(
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        agent=agent,
        user_id=user_id,
    )

    response = client.delete(
        f"/api/conversations/{conversation_id}",
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 404
    assert runtime.conversations.get_conversation(conversation_id) is not None


def test_delete_missing_conversation_returns_not_found(client) -> None:
    token = _login(client)
    response = client.delete(
        "/api/conversations/missing",
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 404


def test_delete_conversation_requires_csrf(client) -> None:
    _login(client)
    response = client.delete("/api/conversations/missing")
    assert response.status_code == 400


def test_empty_orphaned_conversation_returns_reconciled_execution(client) -> None:
    from agentkit.web.app import get_runtime

    _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    conversation_id = runtime.conversations.create_conversation(
        tenant_id=tenant_id,
        agent="general_agent",
        user_id="console-admin",
        title="失败会话",
    )
    parent_id = runtime.gateway.audit.start_run(
        tenant_id=tenant_id,
        user_id="console-admin",
        text="你好",
        agent_id="general_agent",
        conversation_id=conversation_id,
    )
    child_id = runtime.gateway.audit.start_run(
        tenant_id=tenant_id,
        user_id="console-admin",
        text="子任务",
        agent_id="customer_service",
        parent_run_id=parent_id,
        conversation_id=conversation_id,
    )
    runtime.gateway.audit.record(
        parent_id,
        "run_paused",
        {"status": "waiting_for_approval", "child_run_id": child_id},
    )
    runtime.gateway.audit.record(
        child_id,
        "run_failed",
        {"error": "private stack and browser diagnostics"},
    )
    runtime.gateway.audit.record(child_id, "run_finished", {"status": "failed"})

    response = client.get(f"/api/conversations/{conversation_id}/messages")
    body = response.get_json()

    assert response.status_code == 200
    assert body["messages"] == []
    assert body["execution"]["status"] == "failed"
    assert body["execution"]["latest_run_id"] == parent_id
    assert body["execution"]["original_request"] == "你好"
    assert body["execution"]["retryable"] is True
    assert body["execution"]["reconciled"] is True
    assert body["execution"]["requires_second_delete_confirmation"] is True
    assert "private stack" not in body["execution"]["reason"]
    assert runtime.gateway.audit.get_run(parent_id)["status"] == "failed"


def test_retry_creates_a_new_run_in_the_same_conversation(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    conversation_id = runtime.conversations.create_conversation(
        tenant_id=tenant_id,
        agent="general_agent",
        user_id="console-admin",
        title="可重试会话",
    )
    old_run_id = runtime.gateway.audit.start_run(
        tenant_id=tenant_id,
        user_id="console-admin",
        text="你好",
        agent_id="general_agent",
        conversation_id=conversation_id,
    )
    runtime.gateway.audit.record(old_run_id, "run_finished", {"status": "failed"})

    response = client.post(
        f"/api/conversations/{conversation_id}/retry/stream",
        headers={"X-CSRF-Token": token},
    )
    final = _sse_final(response)

    assert response.status_code == 200
    assert final["conversation_id"] == conversation_id
    assert final["run_id"] != old_run_id
    assert runtime.gateway.audit.get_run(old_run_id)["status"] == "failed"


def test_reconciled_conversation_requires_termination_endpoint(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    conversation_id = runtime.conversations.create_conversation(
        tenant_id=tenant_id,
        agent="general_agent",
        user_id="console-admin",
    )
    run_id = runtime.gateway.audit.start_run(
        tenant_id=tenant_id,
        user_id="console-admin",
        text="失败任务",
        agent_id="general_agent",
        conversation_id=conversation_id,
    )
    runtime.gateway.audit.record(
        run_id,
        "run_reconciled",
        {"from_status": "running", "to_status": "failed"},
    )
    runtime.gateway.audit.record(run_id, "run_finished", {"status": "failed"})

    ordinary = client.delete(
        f"/api/conversations/{conversation_id}",
        headers={"X-CSRF-Token": token},
    )
    terminated = client.post(
        f"/api/conversations/{conversation_id}/terminate-and-delete",
        headers={"X-CSRF-Token": token},
    )

    assert ordinary.status_code == 409
    assert terminated.status_code == 200
    assert terminated.get_json()["status"] == "deleted"
    assert runtime.conversations.get_conversation(conversation_id) is None


def test_running_conversation_termination_returns_pending(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    conversation_id = runtime.conversations.create_conversation(
        tenant_id=tenant_id,
        agent="general_agent",
        user_id="console-admin",
    )
    run_id = runtime.gateway.audit.start_run(
        tenant_id=tenant_id,
        user_id="console-admin",
        text="长任务",
        agent_id="general_agent",
        conversation_id=conversation_id,
    )

    response = client.post(
        f"/api/conversations/{conversation_id}/terminate-and-delete",
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 202
    assert response.get_json()["status"] == "pending"
    assert runtime.gateway.audit.get_run(run_id)["status"] == "cancellation_requested"
    assert runtime.conversations.get_conversation(conversation_id)["status"] == (
        "deletion_pending"
    )


def test_conversation_list_finalizes_pending_deletion_after_run_stops(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    conversation_id = runtime.conversations.create_conversation(
        tenant_id=tenant_id,
        agent="general_agent",
        user_id="console-admin",
    )
    run_id = runtime.gateway.audit.start_run(
        tenant_id=tenant_id,
        user_id="console-admin",
        text="长任务",
        agent_id="general_agent",
        conversation_id=conversation_id,
    )
    pending = client.post(
        f"/api/conversations/{conversation_id}/terminate-and-delete",
        headers={"X-CSRF-Token": token},
    )
    assert pending.status_code == 202
    runtime.gateway.audit.record(run_id, "run_finished", {"status": "cancelled"})

    response = client.get("/api/conversations")
    rows = response.get_json()["conversations"]

    assert response.status_code == 200
    assert all(row["id"] != conversation_id for row in rows)
    assert runtime.conversations.get_conversation(conversation_id) is None


def test_pending_deletion_conversation_rejects_new_chat_turn(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    conversation_id = runtime.conversations.create_conversation(
        tenant_id=tenant_id,
        agent="general_agent",
        user_id="console-admin",
    )
    assert runtime.conversations.transition_conversation_status(
        conversation_id,
        expected=("active",),
        status="deletion_pending",
    )

    response = client.post(
        "/api/chat",
        json={"message": "继续执行", "conversation_id": conversation_id},
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 400
    assert "正在删除" in response.get_json()["error"]
    assert runtime.gateway.audit.runs_for_conversation(
        conversation_id=conversation_id,
        tenant_id=tenant_id,
        user_id="console-admin",
    ) == []


def test_explicit_mention_applies_to_one_turn_and_trace_keeps_parent_child(client) -> None:
    token = _login(client)
    first = client.post(
        "/api/chat",
        json={"message": "@客服 请帮我处理售后", "skill": "customer.answer"},
        headers={"X-CSRF-Token": token},
    ).get_json()

    assert first["agent"] == "customer_service"
    assert first["response"]["governance"]["route"]["type"] == "explicit_mention"
    child_run_id = first["response"]["governance"]["delegation"]["child_run_id"]

    second = client.post(
        "/api/chat",
        json={
            "message": "继续说明处理原则",
            "skill": "customer.answer",
            "conversation_id": first["conversation_id"],
        },
        headers={"X-CSRF-Token": token},
    ).get_json()

    assert second["response"]["governance"]["route"]["type"] == "general_delegate"
    trace = client.get(f"/api/runs/{first['run_id']}").get_json()
    assert any(run["run_id"] == child_run_id for run in trace["children"])
