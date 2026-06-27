"""Combined intent+route resolves both in a single LLM call.

With AGENTKIT_COMBINED_INTENT_ROUTE=true, an LLM-bound request resolves the
IntentFrame and the routed skill in one round trip instead of two separate
intent + route LLM calls. The fast-path still takes precedence when it can
resolve the route deterministically (zero LLM calls).
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
    if "combined intent-and-routing node" in s:
        return "combined"
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
    if label == "combined":
        return json.dumps(
            {
                "intent_type": "business_task",
                "goal": "rank candidates",
                "target": {"kind": "business_skill", "name": "candidate.rank"},
                "entities": {},
                "confidence": "high",
                "signals": [],
                "route": {
                    "skill_name": "candidate.rank",
                    "reason": "match",
                    "confidence": "high",
                },
            }
        )
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


def _run(monkeypatch, tmp_path, *, combined: bool, fastpath: bool, request: TaskRequest):
    calls: list[str] = []

    def responder(system: str, user: str) -> str:
        label = _label(system)
        calls.append(label)
        return _payload(label)

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=responder))
    monkeypatch.setenv("AGENTKIT_COMBINED_INTENT_ROUTE", "true" if combined else "false")
    monkeypatch.setenv("AGENTKIT_DETERMINISTIC_FASTPATH", "true" if fastpath else "false")
    config_mod.get_settings.cache_clear()
    try:
        runtime = build_runtime(db_path=tmp_path / "audit.sqlite")
        waiting = runtime.gateway.handle(request).to_dict()
        pre_approval = list(calls)
        thread_id = waiting["output"].get("thread_id")
        resumed = None
        if waiting["output"]["status"] == "waiting_for_approval" and thread_id:
            resumed = runtime.gateway.resume(
                thread_id, approved_skills=["candidate.rank"]
            ).to_dict()
        return waiting, pre_approval, resumed
    finally:
        config_mod.get_settings.cache_clear()


_REQUEST = TaskRequest(
    user_id="u-1",
    roles=["recruiter"],
    text="Rank the top candidate for JOB-001.",
    context={"agent": "hr_recruiter", "job_id": "JOB-001", "candidate_ids": ["C-100"], "top_n": 1},
)


def test_combined_collapses_intent_and_route(monkeypatch, tmp_path):
    waiting, pre_approval, resumed = _run(
        monkeypatch, tmp_path, combined=True, fastpath=False, request=_REQUEST
    )

    # One combined call replaces the two separate intent + route calls.
    assert "combined" in pre_approval
    assert not ({"intent", "route"} & set(pre_approval)), pre_approval
    # The rest of the governance pipeline still runs with the LLM.
    assert {"plan", "plan_review"} <= set(pre_approval)

    assert waiting["output"]["status"] == "waiting_for_approval"
    assert resumed is not None
    assert resumed["output"]["governance"]["approval"]["status"] == "approved"
    final = resumed["output"].get("final") or {}
    ranked = final.get("ranked_candidates") or resumed["output"].get("ranked_candidates")
    assert ranked and ranked[0]["candidate_id"] == "C-100"


def test_disabled_uses_separate_intent_and_route(monkeypatch, tmp_path):
    _, pre_approval, _ = _run(
        monkeypatch, tmp_path, combined=False, fastpath=False, request=_REQUEST
    )
    assert {"intent", "route"} <= set(pre_approval)
    assert "combined" not in pre_approval


def test_fastpath_takes_precedence_over_combined(monkeypatch, tmp_path):
    # Both enabled: the deterministic fast-path resolves the route with high
    # confidence, so neither the combined nor the separate LLM calls run.
    _, pre_approval, _ = _run(monkeypatch, tmp_path, combined=True, fastpath=True, request=_REQUEST)
    assert not ({"combined", "intent", "route", "plan", "plan_review"} & set(pre_approval))
