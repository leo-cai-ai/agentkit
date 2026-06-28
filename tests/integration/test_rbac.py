"""Integration tests for console RBAC via reverse-proxy identity headers."""

from __future__ import annotations

import json

import pytest

import agentkit.config as config_mod


def _responder(system: str, user: str) -> str:
    s = system.lower()
    if "intent decomposition module" in s:
        return json.dumps(
            {
                "intent_type": "business_task",
                "goal": "rank",
                "target": {"kind": "none", "name": ""},
                "entities": {},
                "confidence": "high",
                "signals": [],
            }
        )
    if "routing node" in s:
        return json.dumps({"skill_name": "candidate.rank", "reason": "m", "confidence": "high"})
    if "planning node" in s:
        return json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "skill_name": "candidate.rank",
                        "mode": "plan_execute",
                        "depends_on": [],
                    }
                ],
                "warnings": [],
            }
        )
    if "plan-review node" in s or "output-review node" in s:
        return json.dumps({"status": "approved", "reason": "ok", "findings": []})
    if "approval-governance node" in s:
        return json.dumps(
            {
                "risk_level": "low",
                "approval_summary": "ok",
                "concerns": [],
                "recommended_status": "approved",
            }
        )
    if "execute-preflight node" in s:
        return json.dumps({"execution_goal": "rank", "expected_outputs": [], "risks": []})
    if "recruiting assistant" in s:
        return "Recommended hire."
    return "ok"


@pytest.fixture
def client(monkeypatch):
    # Proxy-terminated SSO: identity arrives via trusted headers, no shared token.
    monkeypatch.setenv("AGENTKIT_AUTH_PROXY_ENABLED", "true")
    monkeypatch.setenv("AGENTKIT_RBAC_ROLE_PERMISSIONS", '{"chat_only":["chat:use"]}')
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    monkeypatch.delenv("AGENTKIT_WEB_AUTH_TOKEN", raising=False)
    config_mod.get_settings.cache_clear()

    import agentkit.core.llm_client as llm_client
    from agentkit.llm.fake import FakeProvider

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_responder))

    from agentkit.web.app import app
    from agentkit.web.security import configure_security

    configure_security(app)
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    yield app.test_client()
    config_mod.get_settings.cache_clear()


def _headers(user: str, roles: str) -> dict[str, str]:
    return {"X-Forwarded-User": user, "X-Forwarded-Roles": roles}


def test_viewer_can_view_governance(client):
    resp = client.get("/governance", headers=_headers("vera", "viewer"))
    assert resp.status_code == 200


def test_viewer_can_view_registry(client):
    resp = client.get("/api/registry", headers=_headers("vera", "viewer"))
    assert resp.status_code == 200


def test_member_forbidden_from_registry(client):
    resp = client.get("/api/registry", headers=_headers("mira", "member"))
    assert resp.status_code == 403


def test_viewer_forbidden_from_running_tasks(client):
    resp = client.post(
        "/api/tasks",
        json={"agent": "hr_recruiter", "text": "Rank candidates."},
        headers=_headers("vera", "viewer"),
    )
    assert resp.status_code == 403


def test_chat_use_only_role_cannot_run_action_agent(client):
    resp = client.post(
        "/api/chat",
        json={"context": {"agent": "hr_recruiter", "message": "Rank candidates."}},
        headers=_headers("chris", "chat_only"),
    )
    assert resp.status_code == 403
    assert "task:run" in resp.get_json()["error"]


def test_viewer_forbidden_from_approving(client):
    resp = client.post(
        "/api/tasks/resume",
        json={"thread_id": "x", "approved_skills": ["candidate.rank"]},
        headers=_headers("vera", "viewer"),
    )
    assert resp.status_code == 403


def test_operator_can_run_and_approve(client):
    # Operator has task:run; the task pauses for approval (CSRF is proxy-owned).
    waiting = client.post(
        "/api/tasks",
        json={"agent": "hr_recruiter", "text": "Rank the top candidate for JOB-001."},
        headers=_headers("olly", "operator"),
    )
    assert waiting.status_code == 200
    out = waiting.get_json()["response"]["output"]
    assert out["status"] == "waiting_for_approval"
    thread_id = out["thread_id"]

    done = client.post(
        "/api/tasks/resume",
        json={"thread_id": thread_id, "approved_skills": ["candidate.rank"]},
        headers=_headers("olly", "operator"),
    ).get_json()
    assert done["response"]["output"]["governance"]["approval"]["status"] == "approved"


def test_task_run_rejects_inline_approval_decision(client):
    resp = client.post(
        "/api/tasks",
        json={
            "agent": "hr_recruiter",
            "text": "Rank candidates.",
            "approved_skills": ["candidate.rank"],
        },
        headers=_headers("olly", "operator"),
    )
    assert resp.status_code == 400
    assert "approval decisions are not accepted" in resp.get_json()["error"]


def test_task_run_rejects_non_action_agent(client):
    resp = client.post(
        "/api/tasks",
        json={"agent": "customer_service", "text": "hello"},
        headers=_headers("olly", "operator"),
    )
    assert resp.status_code == 400
    assert "not an enabled action agent" in resp.get_json()["error"]


def test_payload_roles_do_not_become_business_roles(client):
    resp = client.post(
        "/api/tasks",
        json={
            "agent": "hr_recruiter",
            "text": "Rank candidates.",
            "roles": ["hr_admin"],
        },
        headers=_headers("olly", "operator"),
    )
    assert resp.status_code == 200
    runtime_context = resp.get_json()["response"]["output"]["governance"]["runtime_context"]
    assert "hr_admin" not in runtime_context["roles"]
    assert "ignored_payload_roles" in runtime_context["context_keys"]


def test_unidentified_request_blocked(client):
    # No identity header and no shared token configured -> fail closed.
    resp = client.get("/governance")
    assert resp.status_code != 200
