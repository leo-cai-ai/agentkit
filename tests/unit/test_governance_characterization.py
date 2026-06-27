"""Characterization tests: lock current reviewer outputs before refactoring."""

import json

import agentkit.core.llm_client as llm_client
from agentkit.core.contracts import PlanStep, RouteDecision, TaskPlan, TaskRequest
from agentkit.core.governance import OutputReviewer, PlanReviewer
from agentkit.llm.fake import FakeProvider


def _provider(payload: dict):
    return lambda: FakeProvider(responder=lambda s, u: json.dumps(payload))


def _request() -> TaskRequest:
    return TaskRequest(user_id="u", roles=["recruiter"], text="rank")


def _plan(*, steps, warnings=None) -> TaskPlan:
    return TaskPlan(
        route=RouteDecision(skill_name="candidate.rank", reason="r"),
        steps=steps,
        warnings=warnings or [],
    )


def _step() -> PlanStep:
    return PlanStep(step_id=1, skill_name="candidate.rank", mode="plan_execute", args={})


def test_plan_review_approved(monkeypatch):
    monkeypatch.setattr(
        llm_client,
        "_get_provider",
        _provider({"status": "approved", "reason": "ok", "findings": []}),
    )
    result = PlanReviewer({}).review(request=_request(), plan=_plan(steps=[_step()]))
    assert result == {
        "status": "approved",
        "reason": "ok",
        "findings": [],
        "step_count": 1,
        "llm_required": True,
    }


def test_plan_review_promotes_to_warnings_when_plan_has_warnings(monkeypatch):
    monkeypatch.setattr(
        llm_client,
        "_get_provider",
        _provider({"status": "approved", "reason": "ok", "findings": []}),
    )
    result = PlanReviewer({}).review(
        request=_request(), plan=_plan(steps=[_step()], warnings=["heads up"])
    )
    assert result["status"] == "approved_with_warnings"
    assert {"severity": "warning", "message": "heads up"} in result["findings"]


def test_plan_review_empty_plan_rejected_is_normalized_to_skipped(monkeypatch):
    monkeypatch.setattr(
        llm_client,
        "_get_provider",
        _provider({"status": "rejected", "reason": "no", "findings": []}),
    )
    result = PlanReviewer({}).review(request=_request(), plan=_plan(steps=[]))
    assert result["status"] == "skipped"


def test_output_review_approved(monkeypatch):
    monkeypatch.setattr(
        llm_client,
        "_get_provider",
        _provider({"status": "approved", "reason": "ok", "findings": []}),
    )
    output = {"final": {"message": "done"}}
    result = OutputReviewer({}).review(
        request=_request(), plan=_plan(steps=[_step()]), output=output
    )
    assert result == {
        "status": "approved",
        "reason": "ok",
        "findings": [],
        "llm_required": True,
    }


def test_output_review_error_forces_failed(monkeypatch):
    monkeypatch.setattr(
        llm_client,
        "_get_provider",
        _provider({"status": "approved", "reason": "ok", "findings": []}),
    )
    output = {"error": "boom", "reason": "kaboom"}
    result = OutputReviewer({}).review(
        request=_request(), plan=_plan(steps=[_step()]), output=output
    )
    assert result["status"] == "failed"
