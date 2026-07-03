"""统一 Agent Graph 与 Fake LLM 集成测试。"""

from __future__ import annotations

import json

from agentkit.core import llm_client
from agentkit.core.contracts import TaskRequest
from agentkit.llm.fake import FakeProvider
from agentkit.runtime.bootstrap import build_runtime


def _responder(system: str, user: str) -> str:
    if "intent decomposition module" in system.lower():
        payload = json.loads(user)
        message = payload["message"].lower()
        if "hello" in message:
            return json.dumps(
                {
                    "intent_type": "chit_chat",
                    "goal": "友好地回应用户",
                    "target": {"kind": "platform_handler", "name": "default"},
                    "entities": {},
                    "confidence": "high",
                    "signals": [],
                }
            )
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


def test_full_graph_executes_batch_capability(monkeypatch, tmp_path) -> None:
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

    assert response.status == "completed"
    assert response.strategy == "batch"
    assert response.governance["strategy"] == "batch"
    assert response.output["results"][0]["ranked_candidates"][0]["candidate_id"] == "C-100"


def test_full_graph_handles_chitchat_inside_explicit_agent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_responder))
    runtime = build_runtime(db_path=tmp_path / "audit.sqlite")
    response = runtime.gateway.handle(
        TaskRequest(
            user_id="u-2",
            roles=["support_agent"],
            text="hello there",
            context={"agent": "customer_service"},
        )
    )

    assert response.status == "completed"
    assert response.strategy == "direct"
    assert response.output == {"answer": "友好地回应用户"}
