"""Conversation Timeline 与 durable command API 集成测试。"""

from __future__ import annotations

import json

import pytest

import agentkit.config as config_mod
from tests.integration.test_chat_api import _login, _responder


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


def _sse_frames(response) -> list[dict]:
    frames: list[dict] = []
    event = "message"
    for line in response.get_data(as_text=True).splitlines():
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            frames.append({"event": event, "data": json.loads(line.removeprefix("data: "))})
    return frames


def _failed_turn(runtime, *, user_id: str = "console-admin", client_id: str = "failed-1"):
    accepted = runtime.conversation_projection.accept_user_message(
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        user_id=user_id,
        conversation_id=None,
        client_message_id=client_id,
        content="研究小红书 Top 5",
        title="研究小红书 Top 5",
    )
    runtime.conversation_projection.bind_run(
        accepted.attempt_id,
        run_id=f"run-{client_id}",
        agent_id="general_agent",
    )
    runtime.conversation_projection.fail_attempt(
        accepted.attempt_id,
        error_code="publish_failed",
        error_summary="发布失败",
    )
    return accepted


def test_stream_accepts_and_persists_turn_before_agent_failure(client, monkeypatch) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()

    def fail_after_accept(_task):
        raise RuntimeError("agent failed")

    monkeypatch.setattr(runtime.chat_service, "handle", fail_after_accept)
    response = client.post(
        "/api/chat/stream",
        json={"message": "你好", "client_message_id": "client-1"},
        headers={"X-CSRF-Token": token},
    )

    frames = _sse_frames(response)
    assert frames[0]["event"] == "accepted"
    accepted = frames[0]["data"]
    timeline = client.get(f"/api/conversations/{accepted['conversation_id']}/timeline").get_json()
    assert timeline["turns"][0]["user_message"]["content"] == "你好"


def test_short_stream_failure_force_flushes_partial_before_attempt_failure(
    client,
    monkeypatch,
) -> None:
    import agentkit.core.llm_client as llm_client
    from agentkit.llm.fake import FakeProvider

    token = _login(client)
    blocking = FakeProvider(responder=_responder)

    class PartialFailureProvider:
        name = "partial-failure"

        def complete(self, system: str, user: str) -> str:
            if "可选动作" in system and "delegate" in system:
                return json.dumps(
                    {
                        "action": "answer",
                        "target_agent": None,
                        "task": "",
                        "reason": "直接回答",
                        "confidence": "high",
                    }
                )
            return blocking.complete(system, user)

        def stream(self, system: str, user: str):
            del system, user
            yield "短输出"
            raise RuntimeError("provider disconnected")

    monkeypatch.setattr(llm_client, "_get_provider", lambda: PartialFailureProvider())
    response = client.post(
        "/api/chat/stream",
        json={"message": "你好", "client_message_id": "partial-client"},
        headers={"X-CSRF-Token": token},
    )

    frames = _sse_frames(response)
    accepted = next(frame for frame in frames if frame["event"] == "accepted")["data"]
    assert any(frame["event"] == "error" for frame in frames)
    timeline = client.get(f"/api/conversations/{accepted['conversation_id']}/timeline").get_json()
    attempt = timeline["turns"][0]["attempts"][0]
    assert attempt["status"] == "failed"
    assert len(attempt["messages"]) == 1
    assert attempt["messages"][0]["content"] == "短输出"


def test_timeline_can_recover_by_client_message_id_when_accepted_frame_is_lost(client) -> None:
    token = _login(client)
    client_message_id = "lost-accepted-client"
    response = client.post(
        "/api/chat/stream",
        json={"message": "你好", "client_message_id": client_message_id},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200

    recovered = client.get(
        "/api/conversations/timeline",
        query_string={"client_message_id": client_message_id},
    )

    assert recovered.status_code == 200
    assert recovered.get_json()["turns"][0]["client_message_id"] == client_message_id


def test_retry_endpoint_appends_attempt_and_keeps_first_attempt(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    accepted = _failed_turn(runtime)
    response = client.post(
        f"/api/conversation-turns/{accepted.turn_id}/attempts",
        json={
            "retry_of_attempt_id": accepted.attempt_id,
            "idempotency_key": "retry-1",
        },
        headers={"X-CSRF-Token": token},
    )

    frames = _sse_frames(response)
    assert response.status_code == 200
    assert frames[0]["event"] == "accepted"
    assert frames[0]["data"]["attempt_id"] != accepted.attempt_id
    timeline = client.get(f"/api/conversations/{accepted.conversation_id}/timeline").get_json()
    assert len(timeline["turns"][0]["attempts"]) == 2
    assert timeline["turns"][0]["attempts"][0]["id"] == accepted.attempt_id


def test_duplicate_retry_command_does_not_restart_coordinator(client, monkeypatch) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    accepted = _failed_turn(runtime, client_id="duplicate-retry")
    original = runtime.chat_service.handle
    calls = 0

    def counted(task):
        nonlocal calls
        calls += 1
        return original(task)

    monkeypatch.setattr(runtime.chat_service, "handle", counted)
    payload = {
        "retry_of_attempt_id": accepted.attempt_id,
        "idempotency_key": "retry-duplicate",
    }
    first = client.post(
        f"/api/conversation-turns/{accepted.turn_id}/attempts",
        json=payload,
        headers={"X-CSRF-Token": token},
    )
    second = client.post(
        f"/api/conversation-turns/{accepted.turn_id}/attempts",
        json=payload,
        headers={"X-CSRF-Token": token},
    )

    assert first.status_code == second.status_code == 200
    assert calls == 1
    assert (
        _sse_frames(first)[0]["data"]["attempt_id"] == _sse_frames(second)[0]["data"]["attempt_id"]
    )


def test_timeline_rejects_foreign_tenant_and_user_scope(client) -> None:
    from agentkit.web.app import get_runtime

    _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    foreign_user_id = runtime.conversations.create_conversation(
        tenant_id=tenant_id,
        agent="general_agent",
        user_id="not-console-admin",
        title="其他用户",
    )
    foreign_tenant_id = runtime.conversations.create_conversation(
        tenant_id="another-tenant",
        agent="general_agent",
        user_id="console-admin",
        title="其他租户",
    )

    assert client.get(f"/api/conversations/{foreign_user_id}/timeline").status_code == 404
    assert client.get(f"/api/conversations/{foreign_tenant_id}/timeline").status_code == 404


@pytest.mark.parametrize("foreign_kind", ["user", "tenant"])
def test_retry_rejects_foreign_scope_as_not_found(client, foreign_kind) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    accepted = runtime.conversation_projection.accept_user_message(
        tenant_id="another-tenant" if foreign_kind == "tenant" else tenant_id,
        user_id="other-user" if foreign_kind == "user" else "console-admin",
        conversation_id=None,
        client_message_id=f"foreign-{foreign_kind}",
        content="不可见请求",
        title="不可见请求",
    )

    response = client.post(
        f"/api/conversation-turns/{accepted.turn_id}/attempts",
        json={
            "retry_of_attempt_id": accepted.attempt_id,
            "idempotency_key": f"retry-{foreign_kind}",
        },
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 404


@pytest.mark.parametrize("foreign_kind", ["user", "tenant"])
def test_action_decision_rejects_foreign_scope_as_not_found(client, foreign_kind) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    accepted = runtime.conversation_projection.accept_user_message(
        tenant_id="another-tenant" if foreign_kind == "tenant" else tenant_id,
        user_id="other-user" if foreign_kind == "user" else "console-admin",
        conversation_id=None,
        client_message_id=f"foreign-action-{foreign_kind}",
        content="不可见审批",
        title="不可见审批",
    )
    runtime.conversation_projection.bind_run(
        accepted.attempt_id,
        run_id=f"foreign-run-{foreign_kind}",
        agent_id="general_agent",
    )
    action = runtime.conversation_projection.request_approval(
        accepted=accepted,
        run_id=f"foreign-run-{foreign_kind}",
        agent_id="general_agent",
        thread_id=f"thread-{foreign_kind}",
        skills=["customer.answer"],
        preview={"summary": "不可见"},
    )

    response = client.post(
        f"/api/conversation-actions/{action.id}/decision",
        json={
            "decision": "rejected",
            "expected_version": action.version,
            "idempotency_key": f"decision-{foreign_kind}",
        },
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 404


def test_old_retry_route_is_removed_and_messages_has_no_execution_recovery(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    accepted = _failed_turn(runtime, client_id="old-recovery")

    old_retry = client.post(
        f"/api/conversations/{accepted.conversation_id}/retry/stream",
        headers={"X-CSRF-Token": token},
    )
    messages = client.get(f"/api/conversations/{accepted.conversation_id}/messages").get_json()

    assert old_retry.status_code == 404
    assert set(messages) == {"messages"}
