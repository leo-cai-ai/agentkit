"""统一图的关键可追溯事件顺序测试。"""

from __future__ import annotations

import json

import agentkit.core.llm_client as llm_client
from agentkit.core.contracts import TaskRequest
from agentkit.llm.fake import FakeProvider
from agentkit.runtime.bootstrap import build_runtime


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


def test_run_records_governed_event_sequence(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_responder))
    runtime = build_runtime(db_path=tmp_path / "audit.sqlite")
    response = runtime.gateway.handle(
        TaskRequest(
            user_id="u-1",
            roles=["recruiter"],
            text="Rank the top candidate for JOB-001.",
            context={
                "agent": "hr_recruiter",
                "skill": "candidate.rank",
                "job_id": "JOB-001",
                "candidate_ids": ["C-100"],
                "top_n": 1,
            },
        )
    )

    event_types = [event["type"] for event in response.audit_events]
    expected = [
        "agent_loaded",
        "intent_understood",
        "capability_resolved",
        "strategy_selected",
        "strategy_finished",
        "output_reviewed",
        "run_finished",
    ]
    positions = [event_types.index(event_type) for event_type in expected]
    assert positions == sorted(positions)
