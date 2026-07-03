"""统一 Chat API 集成测试。"""

from __future__ import annotations

import json
import re

import pytest

import agentkit.config as config_mod


def _responder(system: str, user: str) -> str:
    if "intent decomposition module" in system.lower():
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
