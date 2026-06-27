"""Web SSE: /api/tasks/stream + /api/tasks/resume/stream deliver token frames."""

from __future__ import annotations

import json
import re

import pytest

import agentkit.config as config_mod

_SUMMARY = "Recommended hire: the strongest candidate based on the scores."


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
        return _SUMMARY
    return "ok"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("AGENTKIT_WEB_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("AGENTKIT_WEB_COOKIE_SECURE", "false")
    monkeypatch.setenv("AGENTKIT_WEB_AUTH_DISABLED", "false")
    # Exercise the full pipeline regardless of any .env optimization flags.
    monkeypatch.setenv("AGENTKIT_DETERMINISTIC_FASTPATH", "false")
    monkeypatch.setenv("AGENTKIT_COMBINED_INTENT_ROUTE", "false")
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


def _parse_frames(raw: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        event = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip(" "))
        if data_lines:
            frames.append((event, json.loads("\n".join(data_lines))))
    return frames


def test_tasks_stream_pauses_then_resume_streams_tokens(client):
    token = _login_and_csrf(client)

    # First call pauses for approval: a final frame, no token frames yet.
    waiting = client.post(
        "/api/tasks/stream",
        json={"agent": "hr_recruiter", "text": "Rank the top candidate for JOB-001."},
        headers={"X-CSRF-Token": token},
    )
    assert waiting.mimetype == "text/event-stream"
    frames = _parse_frames(waiting.get_data(as_text=True))
    finals = [data for event, data in frames if event == "final"]
    assert finals, f"no final frame: {frames}"
    out = finals[-1]["response"]["output"]
    assert out["status"] == "waiting_for_approval"
    thread_id = out["thread_id"]
    assert thread_id

    # Resume streams the recruiting-assistant summary token-by-token.
    resumed = client.post(
        "/api/tasks/resume/stream",
        json={"thread_id": thread_id, "approved_skills": ["candidate.rank"]},
        headers={"X-CSRF-Token": token},
    )
    assert resumed.mimetype == "text/event-stream"
    frames = _parse_frames(resumed.get_data(as_text=True))
    tokens = [data["delta"] for event, data in frames if event == "token"]
    finals = [data for event, data in frames if event == "final"]

    assert len(tokens) > 1, f"expected multiple token frames: {frames}"
    assert "".join(tokens) == _SUMMARY
    assert finals, "missing final frame on resume"
    final_out = finals[-1]["response"]["output"]
    ranked = (final_out.get("final") or {}).get("ranked_candidates") or final_out.get(
        "ranked_candidates"
    )
    assert ranked, f"no ranked output after resume: {final_out}"


def test_tasks_stream_requires_csrf(client):
    client.post("/login", data={"token": "secret-token"})
    resp = client.post("/api/tasks/stream", json={"agent": "hr_recruiter", "text": "x"})
    assert resp.status_code == 400
