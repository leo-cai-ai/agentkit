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
        data = next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
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


def test_regular_chat_cannot_inject_retry_run_relationship(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    conversation_id = runtime.conversations.create_conversation(
        tenant_id=tenant_id,
        agent="general_agent",
        user_id="console-admin",
        title="普通聊天不可伪造重试",
    )
    old_run_id = runtime.gateway.audit.start_run(
        tenant_id=tenant_id,
        user_id="console-admin",
        text="旧问题",
        agent_id="general_agent",
        conversation_id=conversation_id,
    )
    runtime.gateway.audit.record(old_run_id, "run_finished", {"status": "failed"})
    runtime.conversations.add_message(
        conversation_id=conversation_id,
        role="user",
        content="旧问题",
        run_id=old_run_id,
    )
    runtime.conversations.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content="旧结果",
        run_id=old_run_id,
        agent_id="general_agent",
    )

    response = client.post(
        "/api/chat",
        json={
            "message": "新问题",
            "conversation_id": conversation_id,
            "retry_of_run_id": old_run_id,
        },
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 200
    messages = runtime.conversations.all_messages(conversation_id)
    assert len(messages) == 4
    assert [row["content"] for row in messages[:2]] == ["旧问题", "旧结果"]


def test_duplicate_client_message_does_not_start_or_fail_another_run(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    accepted = runtime.conversation_projection.accept_user_message(
        tenant_id=tenant_id,
        user_id="console-admin",
        conversation_id=None,
        client_message_id="duplicate-client",
        content="你好",
        title="你好",
    )
    first_run = runtime.gateway.audit.start_run(
        tenant_id=tenant_id,
        user_id="console-admin",
        text="你好",
        agent_id="general_agent",
        conversation_id=accepted.conversation_id,
    )
    runtime.conversation_projection.bind_run(
        accepted.attempt_id,
        run_id=first_run,
        agent_id="general_agent",
    )

    response = client.post(
        "/api/chat",
        json={
            "conversation_id": accepted.conversation_id,
            "client_message_id": "duplicate-client",
            "message": "你好",
        },
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["run_id"] == first_run
    assert body["response"]["status"] == "running"
    timeline = runtime.conversation_projection.timeline(
        conversation_id=accepted.conversation_id,
        tenant_id=tenant_id,
        user_id="console-admin",
    )
    assert timeline.turns[0]["attempts"][0]["status"] == "running"
    assert (
        len(
            runtime.gateway.audit.runs_for_conversation(
                conversation_id=accepted.conversation_id,
                tenant_id=tenant_id,
                user_id="console-admin",
            )
        )
        == 1
    )


def test_nested_context_client_message_id_is_idempotent(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    first = client.post(
        "/api/chat",
        json={"message": "你好", "context": {"client_message_id": "nested-client"}},
        headers={"X-CSRF-Token": token},
    )
    conversation_id = first.get_json()["conversation_id"]

    second = client.post(
        "/api/chat",
        json={
            "message": "你好",
            "context": {
                "client_message_id": "nested-client",
                "conversation_id": conversation_id,
            },
        },
        headers={"X-CSRF-Token": token},
    )

    assert first.status_code == second.status_code == 200
    timeline = runtime.conversation_projection.timeline(
        conversation_id=conversation_id,
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        user_id="console-admin",
    )
    assert len(timeline.turns) == 1
    assert second.get_json()["run_id"] == first.get_json()["run_id"]


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


def test_running_conversation_force_delete_returns_conflict_without_mutation(
    client,
) -> None:
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

    assert response.status_code == 409
    assert "正在运行" in response.get_json()["error"]
    assert runtime.gateway.audit.get_run(run_id)["status"] == "running"
    assert runtime.conversations.get_conversation(conversation_id)["status"] == "active"


def test_waiting_conversation_force_delete_cancels_parent_and_child(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    conversation_id = runtime.conversations.create_conversation(
        tenant_id=tenant_id,
        agent="general_agent",
        user_id="console-admin",
    )
    parent_id = runtime.gateway.audit.start_run(
        tenant_id=tenant_id,
        user_id="console-admin",
        text="等待审批",
        agent_id="general_agent",
        conversation_id=conversation_id,
    )
    child_id = runtime.gateway.audit.start_run(
        tenant_id=tenant_id,
        user_id="console-admin",
        text="待审批子任务",
        agent_id="xhs_growth",
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
        "run_paused",
        {"status": "waiting_for_approval"},
    )

    response = client.post(
        f"/api/conversations/{conversation_id}/terminate-and-delete",
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "deleted"
    assert runtime.gateway.audit.get_run(parent_id)["status"] == "cancelled"
    assert runtime.gateway.audit.get_run(child_id)["status"] == "cancelled"
    assert runtime.conversations.get_conversation(conversation_id) is None


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
