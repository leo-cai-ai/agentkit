import json

import agentkit.core.llm_client as llm_client
from agentkit.core.contracts import TaskRequest
from agentkit.llm.fake import FakeProvider
from agentkit.runtime.bootstrap import build_runtime


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


def test_full_graph_hr_execute_with_fake(monkeypatch, tmp_path):
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
    out = response.to_dict()

    assert "governance" in out["output"]
    gov = out["output"]["governance"]
    assert gov["approval"]["status"] == "approved"
    assert gov["output_review"]["status"] in {"approved", "approved_with_warnings"}
    # ranked output may live under output.final or output directly depending on
    # executor packaging — accept either, but it MUST be present and correct.
    final = out["output"].get("final") or {}
    ranked = final.get("ranked_candidates") or out["output"].get("ranked_candidates")
    assert ranked, f"no ranked_candidates in output: {out['output']}"
    assert ranked[0]["candidate_id"] == "C-100"


def test_output_review_fail_closed_does_not_return_blocked_payload(monkeypatch, tmp_path):
    def responder(system: str, user: str) -> str:
        if "output-review node" in system.lower():
            return json.dumps({"status": "failed", "reason": "unsafe", "findings": []})
        return _hr_responder(system, user)

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=responder))
    runtime = build_runtime(db_path=tmp_path / "audit-block.sqlite")
    runtime.tenant_config["output_review_policy"] = "block_on_failed"
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
    out = runtime.gateway.handle(request).to_dict()
    assert out["output"]["error"] == "output_review_failed"
    assert (
        out["output"]["final"]["message"]
        == "The response was blocked by output governance review."
    )
    assert "blocked_output" not in out["output"]
    assert "ranked_candidates" not in json.dumps(out["output"], ensure_ascii=False)
    assert "output_blocked" in [event["type"] for event in out["audit_events"]]


def _chitchat_responder(system: str, user: str) -> str:
    s = system.lower()
    if "intent decomposition module" in s:
        return json.dumps(
            {
                "intent_type": "chit_chat",
                "goal": "respond conversationally",
                "target": {"kind": "platform_handler", "name": "default"},
                "entities": {},
                "confidence": "low",
                "signals": [],
            }
        )
    if "routing node" in s:
        return json.dumps({"skill_name": None, "reason": "chit-chat", "confidence": "low"})
    if "planning node" in s:
        return json.dumps({"steps": [], "warnings": []})
    if "plan-review node" in s:
        return json.dumps({"status": "skipped", "reason": "no skill", "findings": []})
    if "approval-governance node" in s:
        return json.dumps(
            {
                "risk_level": "low",
                "approval_summary": "n/a",
                "concerns": [],
                "recommended_status": "approved",
            }
        )
    if "output-review node" in s:
        return json.dumps({"status": "approved", "reason": "ok", "findings": []})
    if "execute-preflight node" in s:
        return json.dumps(
            {"execution_goal": "respond conversationally", "expected_outputs": [], "risks": []}
        )
    # conversation fallback _llm_reply (no distinctive node phrase)
    return "Hello! How can I help?"


def test_full_graph_chitchat_with_fake(monkeypatch, tmp_path):
    monkeypatch.setattr(
        llm_client, "_get_provider", lambda: FakeProvider(responder=_chitchat_responder)
    )
    runtime = build_runtime(db_path=tmp_path / "audit2.sqlite")
    request = TaskRequest(user_id="u-2", roles=[], text="hello there")
    response = runtime.gateway.handle(request)
    out = response.to_dict()
    assert out["output"]["final"]["conversation"] is True
