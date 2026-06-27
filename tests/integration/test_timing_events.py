"""Integration: a full run records node_timing events and a timing summary."""

from __future__ import annotations

import json

import pytest

import agentkit.config as config_mod
import agentkit.core.llm_client as llm_client
from agentkit.core.contracts import TaskRequest
from agentkit.llm.fake import FakeProvider
from agentkit.runtime.bootstrap import build_runtime


@pytest.fixture(autouse=True)
def _full_pipeline_settings(monkeypatch):
    # This module asserts the full LLM pipeline (per-node timings); pin the
    # latency optimizations off so a developer .env cannot change the shape.
    monkeypatch.setenv("AGENTKIT_DETERMINISTIC_FASTPATH", "false")
    monkeypatch.setenv("AGENTKIT_COMBINED_INTENT_ROUTE", "false")
    config_mod.get_settings.cache_clear()
    yield
    config_mod.get_settings.cache_clear()


def _hr_responder(system: str, user: str) -> str:
    s = system.lower()
    if "intent decomposition module" in s:
        return json.dumps(
            {
                "intent_type": "business_task",
                "goal": "rank candidates",
                "target": {"kind": "none", "name": ""},
                "entities": {},
                "confidence": "high",
                "signals": [],
            }
        )
    if "routing node" in s:
        return json.dumps({"skill_name": "candidate.rank", "reason": "match", "confidence": "high"})
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
        return json.dumps(
            {"execution_goal": "rank candidates", "expected_outputs": [], "risks": []}
        )
    if "recruiting assistant" in s:
        return "Recommended hire: top candidate."
    return "ok"


def test_run_records_timing_events(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_hr_responder))
    runtime = build_runtime(db_path=tmp_path / "audit.sqlite")
    request = TaskRequest(
        user_id="u-1",
        roles=["recruiter"],
        text="Rank the top candidate for JOB-001.",
        context={
            "agent": "hr_recruiter",
            "job_id": "JOB-001",
            "candidate_ids": ["C-100"],
            "top_n": 1,
            "approved_skills": ["candidate.rank"],
        },
    )
    response = runtime.gateway.handle(request)

    timing_events = [e for e in response.audit_events if e["type"] == "node_timing"]
    nodes = {e["payload"]["node"] for e in timing_events}
    assert {"understand_intent", "route", "plan", "execute", "review_output"} <= nodes
    assert all(e["payload"]["ok"] is True for e in timing_events)
    assert all(isinstance(e["payload"]["duration_ms"], float) for e in timing_events)

    summary = runtime.gateway.audit.event_timing_summary()
    by_type = {row["event_type"]: row for row in summary}
    assert "node_timing" in by_type
    assert by_type["node_timing"]["count"] >= 5
    assert by_type["node_timing"]["avg_ms"] is not None
