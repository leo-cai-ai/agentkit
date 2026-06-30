"""Integration tests for the conversational /api/chat + conversation endpoints."""

from __future__ import annotations

import re

import pytest

import agentkit.config as config_mod


def _responder(system: str, user: str) -> str:
    s = system.lower()
    if "intent decomposition module" in s:
        return (
            '{"intent_type":"business_task","goal":"rank","target":{"kind":"none","name":""},'
            '"entities":{},"confidence":"high","signals":[]}'
        )
    if "routing node" in s:
        return '{"skill_name":"candidate.rank","reason":"match","confidence":"high"}'
    if "planning node" in s:
        return (
            '{"steps":[{"step_id":1,"skill_name":"candidate.rank","mode":"plan_execute",'
            '"depends_on":[]}],"warnings":[]}'
        )
    if "plan-review node" in s or "output-review node" in s:
        return '{"status":"approved","reason":"ok","findings":[]}'
    if "approval-governance node" in s:
        return (
            '{"risk_level":"low","approval_summary":"ok","concerns":[],'
            '"recommended_status":"approved"}'
        )
    if "execute-preflight node" in s:
        return '{"execution_goal":"rank","expected_outputs":[],"risks":[]}'
    if "recruiting assistant" in s:
        return "Recommended hire."
    return "Hello, how can I help you today?"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    config_mod.get_settings.cache_clear()

    import agentkit.core.llm_client as llm_client
    from agentkit.llm.fake import FakeProvider

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_responder))

    from agentkit.web.app import app, clear_runtime_cache
    from agentkit.web.security import configure_security

    configure_security(app)
    clear_runtime_cache()
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    yield app.test_client()
    clear_runtime_cache()
    config_mod.get_settings.cache_clear()


def _login_and_csrf(client) -> str:
    resp = client.post("/login", data={"token": "secret-token"})
    assert resp.status_code == 302
    page = client.get("/chat")
    return re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()


def test_chat_endpoint_returns_reply_and_conversation(client):
    token = _login_and_csrf(client)
    resp = client.post(
        "/api/chat",
        json={
            "user_id": "browser-user",
            "context": {"agent": "customer_service", "message": "hi there"},
        },
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["mode"] == "answer"
    assert data["agent_kind"] == "answer"
    assert data["assistant_text"] == "Hello, how can I help you today?"
    assert data["conversation_id"]


def test_chat_endpoint_routes_action_agent(client):
    token = _login_and_csrf(client)
    resp = client.post(
        "/api/chat",
        json={"context": {"agent": "hr_recruiter", "message": "Rank candidates."}},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["mode"] == "action"
    assert data["agent_kind"] == "action"
    assert data["conversation_id"]
    assert data["response"]["output"]["status"] == "needs_clarification"
    assert data["response"]["output"]["input_resolution"]["missing_required"] == [
        "job_id",
        "candidate_ids",
    ]
    msgs = client.get(f"/api/conversations/{data['conversation_id']}/messages").get_json()[
        "messages"
    ]
    assert [msg["role"] for msg in msgs] == ["user", "assistant"]


def test_chat_without_csrf_rejected(client):
    client.post("/login", data={"token": "secret-token"})
    resp = client.post("/api/chat", json={"agent": "customer_service", "message": "hi"})
    assert resp.status_code == 400


def test_conversations_list_and_messages(client):
    token = _login_and_csrf(client)
    chat = client.post(
        "/api/chat",
        json={"agent": "customer_service", "message": "remember this"},
        headers={"X-CSRF-Token": token},
    ).get_json()
    cid = chat["conversation_id"]

    listing = client.get("/api/conversations?agent=customer_service").get_json()
    assert any(c["id"] == cid for c in listing["conversations"])

    msgs = client.get(f"/api/conversations/{cid}/messages").get_json()["messages"]
    roles = [m["role"] for m in msgs]
    assert "user" in roles and "assistant" in roles


def test_resume_conversation_keeps_id(client):
    token = _login_and_csrf(client)
    first = client.post(
        "/api/chat",
        json={"agent": "customer_service", "message": "first"},
        headers={"X-CSRF-Token": token},
    ).get_json()
    cid = first["conversation_id"]
    second = client.post(
        "/api/chat",
        json={"agent": "customer_service", "message": "second", "conversation_id": cid},
        headers={"X-CSRF-Token": token},
    ).get_json()
    assert second["conversation_id"] == cid
