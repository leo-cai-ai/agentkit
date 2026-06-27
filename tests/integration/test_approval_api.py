"""Web API: approval pause (/api/tasks) + in-place resume (/api/tasks/resume)."""

from __future__ import annotations

import json
import re

import pytest

import agentkit.config as config_mod


def _hr_responder(system: str, user: str) -> str:
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
        return "Recommended hire: top candidate."
    return "ok"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    config_mod.get_settings.cache_clear()

    import agentkit.core.llm_client as llm_client
    from agentkit.llm.fake import FakeProvider

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_hr_responder))

    from agentkit.web.app import app
    from agentkit.web.security import configure_security

    configure_security(app)
    yield app.test_client()
    config_mod.get_settings.cache_clear()


def _login_and_csrf(client) -> str:
    assert client.post("/login", data={"token": "secret-token"}).status_code == 302
    page = client.get("/command")
    return re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()


def test_task_pauses_then_resume_completes(client):
    token = _login_and_csrf(client)
    waiting = client.post(
        "/api/tasks",
        json={"agent": "hr_recruiter", "text": "Rank the top candidate for JOB-001."},
        headers={"X-CSRF-Token": token},
    ).get_json()
    assert waiting["response"]["output"]["status"] == "waiting_for_approval"
    thread_id = waiting["response"]["output"]["thread_id"]
    assert thread_id

    done = client.post(
        "/api/tasks/resume",
        json={"thread_id": thread_id, "approved_skills": ["candidate.rank"]},
        headers={"X-CSRF-Token": token},
    ).get_json()
    out = done["response"]["output"]
    final = out.get("final") or {}
    ranked = final.get("ranked_candidates") or out.get("ranked_candidates")
    assert ranked, f"no ranked output after resume: {out}"
    assert out["governance"]["approval"]["status"] == "approved"


def test_resume_requires_thread_id(client):
    token = _login_and_csrf(client)
    resp = client.post(
        "/api/tasks/resume",
        json={"approved_skills": ["candidate.rank"]},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 400


def test_resume_without_csrf_rejected(client):
    client.post("/login", data={"token": "secret-token"})
    resp = client.post("/api/tasks/resume", json={"thread_id": "x"})
    assert resp.status_code == 400
