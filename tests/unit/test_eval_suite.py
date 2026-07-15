"""企业评测套件、报告持久化与基线比较测试。"""

from __future__ import annotations

import json

import pytest

from agentkit.eval import CheckSpec, EvalCase, EvalReport, run_eval
from agentkit.eval.report import build_run_report, load_run_report, write_run_report
from agentkit.eval.suite import (
    filter_cases,
    load_eval_suite,
    load_suite_cases,
    validate_suite_cases,
)


def test_load_suite_resolves_datasets_and_filters_cases(tmp_path) -> None:
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "smoke",
                        "tags": ["smoke", "pr"],
                        "checks": [{"type": "contains", "value": "ok"}],
                    }
                ),
                json.dumps(
                    {
                        "id": "slow",
                        "tags": ["nightly"],
                        "checks": [{"type": "contains", "value": "ok"}],
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(
        """
id: pr-fast
version: 1
target: llm
datasets:
  - cases.jsonl
filters:
  include_tags: [pr]
execution:
  repetitions: 2
  concurrency: 3
gates:
  min_pass_rate: 0.95
  min_mean_score: 0.8
  require_judge: true
""".strip(),
        encoding="utf-8",
    )

    suite = load_eval_suite(suite_path)
    cases = load_suite_cases(suite, suite_path=suite_path)

    assert suite.id == "pr-fast"
    assert suite.execution.repetitions == 2
    assert suite.gates.require_judge is True
    assert [case.id for case in cases] == ["smoke"]


def test_suite_rejects_invalid_threshold(tmp_path) -> None:
    path = tmp_path / "suite.yaml"
    path.write_text(
        "id: broken\nversion: 1\ntarget: llm\ndatasets: [cases.jsonl]\n"
        "gates:\n  min_pass_rate: 1.5\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="min_pass_rate"):
        load_eval_suite(path)


def test_suite_validation_rejects_cases_without_checks() -> None:
    with pytest.raises(ValueError, match="no executable checks"):
        validate_suite_cases([EvalCase(id="empty")])


def test_filter_cases_supports_ids_and_exclusions() -> None:
    cases = [
        EvalCase(id="a", tags=("pr",)),
        EvalCase(id="b", tags=("pr", "slow")),
        EvalCase(id="c", tags=("nightly",)),
    ]

    selected = filter_cases(
        cases,
        include_tags=("pr",),
        exclude_tags=("slow",),
        case_ids=("a", "b"),
    )

    assert [case.id for case in selected] == ["a"]


def test_report_is_versioned_persisted_and_compared_with_baseline(tmp_path) -> None:
    baseline_eval = run_eval(
        [EvalCase(id="a", checks=(CheckSpec("contains", "ok"),))],
        target=lambda case: "ok",
    )
    baseline = build_run_report(
        baseline_eval,
        suite_id="suite",
        suite_version="1",
        target="llm",
        min_pass_rate=1.0,
        min_mean_score=0.0,
        dataset_paths=(),
    )
    baseline_path = tmp_path / "baseline.json"
    write_run_report(baseline, baseline_path)

    current_eval = EvalReport(
        results=tuple(
            run_eval(
                [EvalCase(id="a", checks=(CheckSpec("contains", "ok"),))],
                target=lambda case: "bad",
            ).results
        )
    )
    current = build_run_report(
        current_eval,
        suite_id="suite",
        suite_version="1",
        target="llm",
        min_pass_rate=1.0,
        min_mean_score=0.0,
        dataset_paths=(),
        baseline_path=baseline_path,
    )
    output_path = tmp_path / "current.json"
    write_run_report(current, output_path)
    loaded = load_run_report(output_path)

    assert loaded["schema_version"] == "1.0"
    assert loaded["gate"]["passed"] is False
    assert loaded["baseline_diff"]["pass_rate_delta"] == -1.0
