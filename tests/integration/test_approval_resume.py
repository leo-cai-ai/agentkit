"""Approval pause/resume must not recompute intent/route/plan (perf-critical).

The whole point of the checkpointer is that approving a paused run resumes it
in place, instead of re-running the entire graph (which previously redid the
intent/route/plan/plan_review/approval LLM calls).
"""

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
    # These tests assert the full pre-approval pipeline (intent/route/plan/
    # plan_review) runs; pin the latency optimizations off so a developer .env
    # cannot change which nodes execute. Tests that need a specific checkpointer
    # set it themselves after this fixture.
    monkeypatch.setenv("AGENTKIT_DETERMINISTIC_FASTPATH", "false")
    monkeypatch.setenv("AGENTKIT_COMBINED_INTENT_ROUTE", "false")
    config_mod.get_settings.cache_clear()
    yield
    config_mod.get_settings.cache_clear()


def _label(system: str) -> str:
    s = system.lower()
    if "intent decomposition module" in s:
        return "intent"
    if "routing node" in s:
        return "route"
    if "planning node" in s:
        return "plan"
    if "plan-review node" in s:
        return "plan_review"
    if "approval-governance node" in s:
        return "approval"
    if "output-review node" in s:
        return "output_review"
    if "execute-preflight node" in s:
        return "execute_preflight"
    if "recruiting assistant" in s:
        return "ranking_summary"
    return "other"


def _payload(label: str) -> str:
    if label == "intent":
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
    if label == "route":
        return json.dumps({"skill_name": "candidate.rank", "reason": "match", "confidence": "high"})
    if label == "plan":
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
    if label in {"plan_review", "output_review"}:
        return json.dumps({"status": "approved", "reason": "ok", "findings": []})
    if label == "approval":
        return json.dumps(
            {
                "risk_level": "low",
                "approval_summary": "ok",
                "concerns": [],
                "recommended_status": "approved",
            }
        )
    if label == "execute_preflight":
        return json.dumps({"execution_goal": "rank", "expected_outputs": [], "risks": []})
    if label == "ranking_summary":
        return "Recommended hire: top candidate."
    return "ok"


def test_resume_does_not_recompute_planning(monkeypatch, tmp_path):
    calls: list[str] = []

    def responder(system: str, user: str) -> str:
        label = _label(system)
        calls.append(label)
        return _payload(label)

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=responder))
    runtime = build_runtime(db_path=tmp_path / "audit.sqlite")
    gateway = runtime.gateway

    request = TaskRequest(
        user_id="u-1",
        roles=["recruiter"],
        text="Rank the top candidate for JOB-001.",
        context={
            "agent": "hr_recruiter",
            "job_id": "JOB-001",
            "candidate_ids": ["C-100"],
            "top_n": 1,
        },
    )

    # Phase 1: submit -> pauses for approval.
    waiting = gateway.handle(request).to_dict()
    assert waiting["output"]["status"] == "waiting_for_approval"
    thread_id = waiting["output"]["thread_id"]
    assert thread_id
    phase1 = list(calls)
    assert {"intent", "route", "plan", "plan_review"} <= set(phase1)

    # Phase 2: approve -> resumes in place, must NOT recompute planning nodes.
    calls.clear()
    resumed = gateway.resume(thread_id, approved_skills=["candidate.rank"]).to_dict()
    phase2 = set(calls)

    assert not (
        {"intent", "route", "plan", "plan_review"} & phase2
    ), f"resume recomputed planning nodes: {phase2}"
    # The advisory approval LLM call is also skipped once the human has decided.
    assert "approval" not in phase2
    # And the run actually executed after approval.
    final = resumed["output"].get("final") or {}
    ranked = final.get("ranked_candidates") or resumed["output"].get("ranked_candidates")
    assert ranked and ranked[0]["candidate_id"] == "C-100"
    assert resumed["output"]["governance"]["approval"]["status"] == "approved"


def test_reject_resume_stops_execution(monkeypatch, tmp_path):
    def responder(system: str, user: str) -> str:
        return _payload(_label(system))

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=responder))
    runtime = build_runtime(db_path=tmp_path / "audit2.sqlite")
    gateway = runtime.gateway

    request = TaskRequest(
        user_id="u-1",
        roles=["recruiter"],
        text="Rank the top candidate for JOB-001.",
        context={"agent": "hr_recruiter", "job_id": "JOB-001", "candidate_ids": ["C-100"]},
    )
    waiting = gateway.handle(request).to_dict()
    thread_id = waiting["output"]["thread_id"]

    rejected = gateway.resume(thread_id, rejected_skills=["candidate.rank"]).to_dict()
    assert rejected["output"]["status"] == "rejected"
    final = rejected["output"].get("final") or {}
    assert not (final.get("ranked_candidates") or rejected["output"].get("ranked_candidates"))


def test_sqlite_checkpointer_resumes_across_restart(monkeypatch, tmp_path):
    """A paused approval persisted to sqlite must be resumable by a fresh gateway.

    This proves the on-disk checkpointer survives a process/worker restart: we
    pause on one runtime, then build a brand-new runtime sharing the same data
    directory and resume the same ``thread_id`` to completion.
    """

    def responder(system: str, user: str) -> str:
        return _payload(_label(system))

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=responder))
    monkeypatch.setenv("AGENTKIT_APPROVAL_CHECKPOINTER", "sqlite")
    config_mod.get_settings.cache_clear()

    db_path = tmp_path / "audit.sqlite"
    request = TaskRequest(
        user_id="u-1",
        roles=["recruiter"],
        text="Rank the top candidate for JOB-001.",
        context={
            "agent": "hr_recruiter",
            "job_id": "JOB-001",
            "candidate_ids": ["C-100"],
            "top_n": 1,
        },
    )

    try:
        # Process A: submit -> pauses for approval, checkpoint flushed to disk.
        runtime_a = build_runtime(db_path=db_path)
        waiting = runtime_a.gateway.handle(request).to_dict()
        assert waiting["output"]["status"] == "waiting_for_approval"
        thread_id = waiting["output"]["thread_id"]
        assert thread_id
        # On-disk checkpoint store is created next to the tenant db.
        assert list(tmp_path.glob("*_checkpoints.sqlite"))

        # Process B: a fresh runtime (new graph + new saver over the same file)
        # resumes the paused thread without re-running the planning nodes.
        runtime_b = build_runtime(db_path=db_path)
        resumed = runtime_b.gateway.resume(thread_id, approved_skills=["candidate.rank"]).to_dict()
        assert resumed["output"]["governance"]["approval"]["status"] == "approved"
        final = resumed["output"].get("final") or {}
        ranked = final.get("ranked_candidates") or resumed["output"].get("ranked_candidates")
        assert ranked and ranked[0]["candidate_id"] == "C-100"
    finally:
        config_mod.get_settings.cache_clear()
