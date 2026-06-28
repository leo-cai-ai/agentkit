"""Unit tests for the LLM evaluation harness (agentkit.eval)."""

from __future__ import annotations

import json

import pytest

from agentkit.eval import (
    CheckSpec,
    EvalCase,
    LLMJudge,
    load_cases,
    make_gateway_trace_target,
    run_check,
    run_eval,
)
from agentkit.eval.targets import extract_text

# --- deterministic checks --------------------------------------------------- #


def test_contains_and_not_contains() -> None:
    assert run_check(CheckSpec("contains", "cat"), "the cat sat").passed
    assert not run_check(CheckSpec("contains", "dog"), "the cat sat").passed
    assert run_check(CheckSpec("not_contains", "dog"), "the cat sat").passed


def test_regex_and_equals_and_lengths() -> None:
    assert run_check(CheckSpec("regex", r"\d{3}"), "id 123").passed
    assert run_check(CheckSpec("equals", "hi"), "  hi  ").passed
    assert run_check(CheckSpec("min_length", 3), "abcd").passed
    assert not run_check(CheckSpec("max_length", 2), "abcd").passed


def test_no_pii_check_uses_safety() -> None:
    assert run_check(CheckSpec("no_pii"), "nothing private here").passed
    bad = run_check(CheckSpec("no_pii"), "email me at a@b.com")
    assert not bad.passed
    assert "email" in bad.detail


def test_unknown_check_type_fails_gracefully() -> None:
    out = run_check(CheckSpec("does_not_exist"), "x")
    assert not out.passed
    assert "unknown check type" in out.detail


def test_json_path_and_event_sequence_checks() -> None:
    output = json.dumps(
        {
            "status": "waiting_for_approval",
            "audit_event_types": ["run_started", "context_prepared", "run_paused"],
        }
    )
    assert run_check(CheckSpec("json_path_exists", "status"), output).passed
    assert run_check(
        CheckSpec("json_path_equals", {"path": "status", "equals": "waiting_for_approval"}),
        output,
    ).passed
    assert run_check(
        CheckSpec("event_sequence", ["run_started", "run_paused"]),
        output,
    ).passed


# --- judge ------------------------------------------------------------------ #


def test_judge_passes_with_high_score() -> None:
    judge = LLMJudge(judge_fn=lambda system, user: {"score": 5, "reason": "great"})
    outcome = run_check(CheckSpec("judge", rubric="be helpful", min_score=4), "answer", judge=judge)
    assert outcome.passed
    assert outcome.score == pytest.approx(1.0)


def test_judge_fails_below_min_score() -> None:
    judge = LLMJudge(judge_fn=lambda system, user: {"score": 2, "reason": "weak"})
    outcome = run_check(CheckSpec("judge", rubric="be helpful", min_score=4), "answer", judge=judge)
    assert not outcome.passed


def test_judge_check_skipped_when_no_judge() -> None:
    outcome = run_check(CheckSpec("judge", rubric="x"), "answer", judge=None)
    assert outcome.skipped is True
    assert outcome.passed is False


def test_judge_handles_provider_error() -> None:
    def boom(system: str, user: str) -> dict:
        raise RuntimeError("no llm")

    result = LLMJudge(judge_fn=boom).score(output="x", rubric="r")
    assert result.score == 0.0
    assert "judge error" in result.reason


# --- runner / report -------------------------------------------------------- #


def test_run_eval_aggregates_pass_and_fail() -> None:
    cases = [
        EvalCase(id="a", checks=(CheckSpec("contains", "ok"),)),
        EvalCase(id="b", checks=(CheckSpec("contains", "missing"),)),
    ]
    report = run_eval(cases, target=lambda case: "ok result")
    assert report.total == 2
    assert report.passed_count == 1
    assert report.pass_rate == pytest.approx(0.5)
    assert "FAIL" in report.format_text()


def test_gate_thresholds() -> None:
    cases = [EvalCase(id="a", checks=(CheckSpec("contains", "ok"),))]
    report = run_eval(cases, target=lambda case: "ok")
    assert report.gate(min_pass_rate=1.0) is True
    failing = run_eval(cases, target=lambda case: "nope")
    assert failing.gate(min_pass_rate=1.0) is False


def test_target_exception_is_isolated_as_failure() -> None:
    def boom(case: EvalCase) -> str:
        raise ValueError("kaboom")

    report = run_eval([EvalCase(id="a", checks=(CheckSpec("contains", "x"),))], target=boom)
    assert report.passed_count == 0
    assert "target raised" in report.results[0].outcomes[0].detail


def test_skipped_only_case_counts_as_passed() -> None:
    # A judge-only case with no judge -> skipped -> vacuously passing (not a regression).
    report = run_eval(
        [EvalCase(id="a", checks=(CheckSpec("judge", rubric="x"),))], target=lambda c: "y"
    )
    assert report.results[0].passed is True


# --- dataset loading -------------------------------------------------------- #


def test_load_cases_jsonl(tmp_path) -> None:
    path = tmp_path / "d.jsonl"
    path.write_text(
        "# comment\n"
        + json.dumps({"id": "c1", "user": "hi", "checks": [{"type": "contains", "value": "hi"}]})
        + "\n\n",
        encoding="utf-8",
    )
    cases = load_cases(path)
    assert len(cases) == 1
    assert cases[0].id == "c1"
    assert cases[0].checks[0].type == "contains"


def test_load_cases_json_list(tmp_path) -> None:
    path = tmp_path / "d.json"
    path.write_text(json.dumps([{"id": "c1", "user": "hi"}]), encoding="utf-8")
    assert load_cases(path)[0].id == "c1"


# --- gateway text extraction ------------------------------------------------ #


def test_extract_text_prefers_final_message() -> None:
    assert extract_text({"output": {"final": {"message": "hello"}}}) == "hello"


def test_extract_text_falls_back_to_json() -> None:
    text = extract_text({"output": {"ranked_candidates": [{"name": "Ann"}]}})
    assert "Ann" in text


def test_gateway_trace_target_can_resume_without_leaking_eval_control_context() -> None:
    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def to_dict(self):
            return self._payload

    class _Gateway:
        def __init__(self):
            self.request_context = {}
            self.resumed = False

        def handle(self, request):
            self.request_context = dict(request.context)
            return _Response(
                {
                    "output": {"status": "waiting_for_approval", "thread_id": "t1"},
                    "audit_events": [{"type": "run_paused"}],
                }
            )

        def resume(self, thread_id, *, approved_skills, rejected_skills, decision_context):
            self.resumed = True
            assert thread_id == "t1"
            assert approved_skills == ["candidate.rank"]
            assert rejected_skills == []
            assert decision_context["source"] == "eval"
            return _Response(
                {
                    "output": {"final": {"message": "done"}},
                    "audit_events": [
                        {"type": "run_paused"},
                        {"type": "run_resumed"},
                        {"type": "run_finished"},
                    ],
                }
            )

    class _Runtime:
        def __init__(self):
            self.gateway = _Gateway()

    runtime = _Runtime()
    target = make_gateway_trace_target(runtime)
    case = EvalCase(
        id="resume",
        user="rank",
        context={"_eval_resume": {"approved_skills": ["candidate.rank"]}},
    )
    data = json.loads(target(case))
    assert runtime.gateway.resumed is True
    assert "_eval_resume" not in runtime.gateway.request_context
    assert data["initial_status"] == "waiting_for_approval"
    assert data["status"] == "completed"
