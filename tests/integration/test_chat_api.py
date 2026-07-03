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
def client(monkeypatch):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    config_mod.get_settings.cache_clear()

    import agentkit.core.llm_client as llm_client
    from agentkit.llm.fake import FakeProvider
    from agentkit.web.app import app, clear_runtime_cache
    from agentkit.web.security import configure_security

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_responder))
    configure_security(app)
    clear_runtime_cache()
    yield app.test_client()
    clear_runtime_cache()
    config_mod.get_settings.cache_clear()


def _login(client) -> str:
    assert client.post("/login", data={"token": "secret-token"}).status_code == 302
    page = client.get("/chat")
    return re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()


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
