"""统一 Web API 的控制台 RBAC 与业务角色隔离测试。"""

from __future__ import annotations

import json

import pytest

import agentkit.config as config_mod


def _responder(system: str, user: str) -> str:
    if "intent decomposition module" in system.lower():
        return json.dumps(
            {
                "intent_type": "business_task",
                "goal": "排序候选人",
                "target": {"kind": "business_skill", "name": "candidate.rank"},
                "entities": {},
                "confidence": "high",
                "signals": [],
            }
        )
    return "{}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTKIT_AUTH_PROXY_ENABLED", "true")
    monkeypatch.setenv("AGENTKIT_RBAC_ROLE_PERMISSIONS", '{"chat_only":["chat:use"]}')
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    monkeypatch.delenv("AGENTKIT_WEB_AUTH_TOKEN", raising=False)
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


def _headers(user: str, roles: str) -> dict[str, str]:
    return {"X-Forwarded-User": user, "X-Forwarded-Roles": roles}


def _task_payload() -> dict:
    return {
        "agent": "hr_recruiter",
        "text": "Rank candidate C-100 for JOB-001.",
        "skill": "candidate.rank",
        "job_id": "JOB-001",
        "candidate_ids": ["C-100"],
        "top_n": 1,
    }


def test_viewer_can_view_governance_but_cannot_run(client) -> None:
    headers = _headers("vera", "viewer")
    assert client.get("/governance", headers=headers).status_code == 200
    assert client.get("/api/registry", headers=headers).status_code == 200
    assert client.post("/api/tasks", json=_task_payload(), headers=headers).status_code == 403


def test_member_forbidden_from_registry(client) -> None:
    assert client.get("/api/registry", headers=_headers("mira", "member")).status_code == 403


def test_chat_only_role_can_use_unified_chat_but_not_task_endpoint(client) -> None:
    headers = _headers("chris", "chat_only")
    chat = client.post(
        "/api/chat",
        json={"agent": "customer_service", "message": "你好", "skill": "customer.answer"},
        headers=headers,
    )
    assert chat.status_code == 200
    assert client.post("/api/tasks", json=_task_payload(), headers=headers).status_code == 403


def test_operator_can_run_unified_task(client) -> None:
    response = client.post(
        "/api/tasks",
        json=_task_payload(),
        headers=_headers("olly", "operator"),
    )
    assert response.status_code == 200
    assert response.get_json()["response"]["status"] == "completed"


def test_payload_roles_never_become_business_roles(client, monkeypatch) -> None:
    from agentkit.web.app import get_runtime

    runtime = get_runtime()
    original = runtime.gateway.handle
    observed: list[list[str]] = []

    def capture(request):
        observed.append(request.roles)
        return original(request)

    monkeypatch.setattr(runtime.gateway, "handle", capture)
    payload = {**_task_payload(), "roles": ["hr_admin"]}
    response = client.post(
        "/api/tasks",
        json=payload,
        headers=_headers("olly", "operator"),
    )
    assert response.status_code == 200
    assert "hr_admin" not in observed[0]


def test_unidentified_request_is_blocked(client) -> None:
    assert client.get("/governance").status_code != 200
