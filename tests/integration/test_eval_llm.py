"""Integration test: eval harness end-to-end with llm_target + real LLMJudge."""

from __future__ import annotations

import json

import pytest

import agentkit.config as config_mod
from agentkit.eval import CheckSpec, EvalCase, LLMJudge, llm_target, run_eval


def _responder(system: str, user: str) -> str:
    if "evaluation judge" in system.lower():
        return json.dumps({"score": 5, "reason": "clear and helpful"})
    return "Hello there, happy to help!"


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    config_mod.get_settings.cache_clear()
    import agentkit.core.llm_client as llm_client
    from agentkit.llm.fake import FakeProvider

    monkeypatch.setattr(llm_client, "_get_provider", lambda: FakeProvider(responder=_responder))
    yield
    config_mod.get_settings.cache_clear()


def test_eval_llm_target_with_judge_passes_gate() -> None:
    cases = [
        EvalCase(
            id="greeting",
            system="You are concise.",
            user="Say hi.",
            checks=(CheckSpec("min_length", 2), CheckSpec("no_pii")),
        ),
        EvalCase(
            id="quality",
            system="You are helpful.",
            user="How can you help?",
            checks=(CheckSpec("judge", rubric="Answer is helpful.", min_score=4, weight=2),),
        ),
    ]
    report = run_eval(cases, llm_target, judge=LLMJudge())
    assert report.pass_rate == pytest.approx(1.0)
    assert report.gate(min_pass_rate=1.0, min_mean_score=0.8) is True


def test_eval_detects_regression() -> None:
    # A case whose expectation the stubbed model cannot satisfy -> gate fails.
    cases = [EvalCase(id="needs-keyword", user="x", checks=(CheckSpec("contains", "PINEAPPLE"),))]
    report = run_eval(cases, llm_target, judge=None)
    assert report.gate(min_pass_rate=1.0) is False
