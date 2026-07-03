"""统一 Web API 的副作用审批与原位恢复测试。"""

from __future__ import annotations

import json
import re

import pytest

import agentkit.config as config_mod


def _responder(system: str, user: str) -> str:
    if "意图分解节点" in system:
        return json.dumps(
            {
                "intent_type": "business_task",
                "goal": "申请退款",
                "target": {"kind": "business_skill", "name": "refund.apply"},
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


def _waiting(client, token: str) -> dict:
    return client.post(
        "/api/tasks",
        json={
            "agent": "customer_service",
            "text": "请给订单 O-100 退款",
            "skill": "refund.apply",
            "order_id": "O-100",
        },
        headers={"X-CSRF-Token": token},
    ).get_json()


def test_side_effect_pauses_then_resume_completes(client) -> None:
    token = _login(client)
    waiting = _waiting(client, token)
    assert waiting["response"]["status"] == "waiting_for_approval"

    completed = client.post(
        "/api/tasks/resume",
        json={
            "thread_id": waiting["response"]["thread_id"],
            "approved_skills": ["refund.apply"],
        },
        headers={"X-CSRF-Token": token},
    ).get_json()
    assert completed["response"]["status"] == "completed"
    assert completed["response"]["output"]["status"] == "submitted"


def test_rejection_does_not_execute_side_effect(client) -> None:
    token = _login(client)
    waiting = _waiting(client, token)
    rejected = client.post(
        "/api/tasks/resume",
        json={
            "thread_id": waiting["response"]["thread_id"],
            "rejected_skills": ["refund.apply"],
        },
        headers={"X-CSRF-Token": token},
    ).get_json()
    assert rejected["response"]["status"] == "rejected"


def test_resume_rejects_overlapping_decision(client) -> None:
    token = _login(client)
    response = client.post(
        "/api/tasks/resume",
        json={
            "thread_id": "unknown",
            "approved_skills": ["refund.apply"],
            "rejected_skills": ["refund.apply"],
        },
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 400
    assert "同时批准和拒绝" in response.get_json()["error"]


def test_resume_requires_thread_id(client) -> None:
    token = _login(client)
    response = client.post(
        "/api/tasks/resume",
        json={"approved_skills": ["refund.apply"]},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 400
