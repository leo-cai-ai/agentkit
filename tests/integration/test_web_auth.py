"""Integration tests for web console auth, CSRF, and security headers."""

from __future__ import annotations

import json
import re

import pytest

import agentkit.config as config_mod


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    # Pin (don't just delete) so a local .env with AGENTKIT_WEB_AUTH_DISABLED=true
    # cannot leak in via pydantic-settings' .env file loading.
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    config_mod.get_settings.cache_clear()

    from agentkit.web.app import app
    from agentkit.web.security import configure_security

    configure_security(app)
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    yield app.test_client()
    config_mod.get_settings.cache_clear()


def _login(client) -> None:
    resp = client.post("/login", data={"token": "secret-token"})
    assert resp.status_code == 302


def test_governance_page_shows_context_hash_not_prompt_content(client) -> None:
    _login(client)

    response = client.get("/governance")

    assert response.status_code == 200
    assert b"runtime.intent" in response.data
    assert b"sha256:" in response.data
    assert b"UNTRUSTED_DATA_BEGIN" not in response.data


def test_unauthenticated_redirects_to_login(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_wrong_token_rejected(client):
    resp = client.post("/login", data={"token": "nope"})
    assert resp.status_code == 401


def test_login_then_access_ok_with_security_headers(client):
    _login(client)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in resp.headers
    assert resp.headers["Referrer-Policy"] == "no-referrer"


def test_post_without_csrf_rejected(client):
    _login(client)
    resp = client.post("/api/tasks", json={"text": "hi"})
    assert resp.status_code == 400


def test_post_with_csrf_not_rejected(client, monkeypatch):
    import agentkit.core.llm_client as llm_client
    from agentkit.llm.fake import FakeProvider

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_responder))

    _login(client)
    page = client.get("/chat")
    token = re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()

    resp = client.post(
        "/api/tasks",
        json={"text": "Rank candidates", "agent": "hr_recruiter"},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code != 400


def test_admin_reload_requires_csrf_and_succeeds(client):
    _login(client)
    page = client.get("/chat")
    token = re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
    resp = client.post("/api/admin/reload", headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "reloaded"


def test_auth_disabled_allows_access(monkeypatch):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "true")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "k")
    monkeypatch.delenv("AGENTKIT_WEB_AUTH_TOKEN", raising=False)
    config_mod.get_settings.cache_clear()
    from agentkit.web.app import app
    from agentkit.web.security import configure_security

    configure_security(app)
    app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=False)
    resp = app.test_client().get("/")
    assert resp.status_code == 200
    config_mod.get_settings.cache_clear()


def _responder(system: str, user: str) -> str:
    s = system.lower()
    if "意图分解节点" in system:
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
    if "plan-review node" in s:
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
    if "output-review node" in s:
        return json.dumps({"status": "approved", "reason": "ok", "findings": []})
    if "execute-preflight node" in s:
        return json.dumps({"execution_goal": "rank", "expected_outputs": [], "risks": []})
    if "recruiting assistant" in s:
        return "Recommended hire."
    return "ok"
