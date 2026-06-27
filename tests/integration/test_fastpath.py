"""Deterministic fast-path skips governance LLM calls on high-confidence routes.

When AGENTKIT_DETERMINISTIC_FASTPATH=true and the rule-based router resolves a
skill with HIGH confidence, the intent/route/plan/plan_review/approval LLM calls
are skipped (the dominant pre-approval latency). Ambiguous requests the router
cannot resolve confidently still run the full LLM pipeline.
"""

from __future__ import annotations

import json

import agentkit.config as config_mod
import agentkit.core.llm_client as llm_client
from agentkit.core.contracts import TaskRequest
from agentkit.llm.fake import FakeProvider
from agentkit.runtime.bootstrap import build_runtime


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


def _run_with_fastpath(monkeypatch, tmp_path, *, enabled: bool, request: TaskRequest):
    calls: list[str] = []

    def responder(system: str, user: str) -> str:
        label = _label(system)
        calls.append(label)
        return _payload(label)

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=responder))
    monkeypatch.setenv("AGENTKIT_DETERMINISTIC_FASTPATH", "true" if enabled else "false")
    # Pin the combined-intent+route flag off so this test isolates the fast-path
    # behaviour regardless of any .env override on the developer's machine.
    monkeypatch.setenv("AGENTKIT_COMBINED_INTENT_ROUTE", "false")
    config_mod.get_settings.cache_clear()
    try:
        runtime = build_runtime(db_path=tmp_path / "audit.sqlite")
        waiting = runtime.gateway.handle(request).to_dict()
        thread_id = waiting["output"].get("thread_id")
        pre_approval_calls = list(calls)
        calls.clear()
        resumed = None
        if waiting["output"]["status"] == "waiting_for_approval" and thread_id:
            resumed = runtime.gateway.resume(
                thread_id, approved_skills=["candidate.rank"]
            ).to_dict()
        return waiting, pre_approval_calls, list(calls), resumed
    finally:
        config_mod.get_settings.cache_clear()


_HIGH_CONF_REQUEST = TaskRequest(
    user_id="u-1",
    roles=["recruiter"],
    text="Rank the top candidate for JOB-001.",
    context={"agent": "hr_recruiter", "job_id": "JOB-001", "candidate_ids": ["C-100"], "top_n": 1},
)


def test_fastpath_skips_pre_approval_llms_on_high_confidence(monkeypatch, tmp_path):
    waiting, pre_approval, post_approval, resumed = _run_with_fastpath(
        monkeypatch, tmp_path, enabled=True, request=_HIGH_CONF_REQUEST
    )

    # Still pauses for approval (candidate.rank requires it)...
    assert waiting["output"]["status"] == "waiting_for_approval"
    # ...but NONE of the advisory governance LLMs ran before approval.
    assert not (
        {"intent", "route", "plan", "plan_review", "approval"} & set(pre_approval)
    ), f"fast-path still called pre-approval LLMs: {pre_approval}"

    # After approval the run executes (and only execution-time LLMs run).
    assert resumed is not None
    assert resumed["output"]["governance"]["approval"]["status"] == "approved"
    final = resumed["output"].get("final") or {}
    ranked = final.get("ranked_candidates") or resumed["output"].get("ranked_candidates")
    assert ranked and ranked[0]["candidate_id"] == "C-100"
    assert not ({"intent", "route", "plan", "plan_review"} & set(post_approval))


def test_fastpath_disabled_runs_full_pipeline(monkeypatch, tmp_path):
    _, pre_approval, _, _ = _run_with_fastpath(
        monkeypatch, tmp_path, enabled=False, request=_HIGH_CONF_REQUEST
    )
    assert {"intent", "route", "plan", "plan_review"} <= set(pre_approval)


def test_fastpath_falls_back_when_route_not_high_confidence(monkeypatch, tmp_path):
    # No action keywords -> deterministic route cannot resolve a skill with high
    # confidence, so the full LLM pipeline must run even with fast-path enabled.
    ambiguous = TaskRequest(
        user_id="u-1",
        roles=["recruiter"],
        text="Please take care of JOB-001 when you can.",
        context={"agent": "hr_recruiter"},
    )
    _, pre_approval, _, _ = _run_with_fastpath(
        monkeypatch, tmp_path, enabled=True, request=ambiguous
    )
    assert {"intent", "route", "plan", "plan_review"} <= set(pre_approval)
