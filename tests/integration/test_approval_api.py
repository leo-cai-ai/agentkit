"""统一 Web API 的副作用审批与原位恢复测试。"""

from __future__ import annotations

import json
import re
import uuid

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


def _waiting(client, token: str) -> dict:
    return client.post(
        "/api/chat",
        json={
            "message": "@客服 请给订单 O-100 退款",
            "client_message_id": str(uuid.uuid4()),
            "skill": "refund.apply",
            "order_id": "O-100",
        },
        headers={"X-CSRF-Token": token},
    ).get_json()


def _action(waiting: dict) -> dict:
    approval = waiting["response"]["output"]["approval"]
    return {"id": approval["action_id"], "version": approval["version"]}


def _decide(client, token: str, action: dict, decision: str, key: str):
    return client.post(
        f"/api/conversation-actions/{action['id']}/decision",
        json={
            "decision": decision,
            "expected_version": action["version"],
            "idempotency_key": key,
        },
        headers={"X-CSRF-Token": token},
    )


def test_side_effect_pauses_then_resume_completes(client) -> None:
    token = _login(client)
    waiting = _waiting(client, token)
    assert waiting["response"]["status"] == "waiting_for_approval"

    action = _action(waiting)
    completed = _decide(client, token, action, "approved", "approve-1").get_json()
    assert completed["response"]["status"] == "completed"
    assert completed["response"]["output"]["status"] == "submitted"

    duplicate = _decide(client, token, action, "approved", "approve-1")
    assert duplicate.status_code == 200
    assert duplicate.get_json()["response"]["status"] == "completed"


def test_rejection_does_not_execute_side_effect(client) -> None:
    token = _login(client)
    waiting = _waiting(client, token)
    rejected = _decide(client, token, _action(waiting), "rejected", "reject-1").get_json()
    assert rejected["response"]["status"] == "rejected"


def test_action_decision_rejects_browser_thread_and_skills(client) -> None:
    token = _login(client)
    waiting = _waiting(client, token)
    action = _action(waiting)
    response = client.post(
        f"/api/conversation-actions/{action['id']}/decision",
        json={
            "decision": "approved",
            "expected_version": action["version"],
            "idempotency_key": "untrusted-fields",
            "thread_id": waiting["response"]["thread_id"],
            "skills": ["refund.apply"],
        },
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 400
    assert "不支持" in response.get_json()["error"]


def test_action_decision_requires_version_and_idempotency_key(client) -> None:
    token = _login(client)
    waiting = _waiting(client, token)
    response = client.post(
        f"/api/conversation-actions/{_action(waiting)['id']}/decision",
        json={"decision": "approved"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 400


def test_legacy_browser_resume_endpoint_is_disabled(client) -> None:
    token = _login(client)
    response = client.post(
        "/api/tasks/resume",
        json={"thread_id": "untrusted", "approved_skills": ["refund.apply"]},
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 410
