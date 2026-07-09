"""策略轨迹数据集的结构与覆盖门禁。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def test_strategy_trajectory_dataset_covers_runtime_matrix() -> None:
    path = Path(__file__).resolve().parents[2] / "evaluation" / "datasets" / "trajectory.jsonl"
    cases = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(cases) >= 12
    assert len({case["id"] for case in cases}) == len(cases)

    counts = Counter(case["expected_strategy"] for case in cases)
    assert counts["direct"] >= 2
    assert counts["workflow"] >= 2
    assert counts["batch"] >= 2
    assert counts["react"] >= 2
    assert counts["plan_execute"] >= 2
    assert any("side-effect" in case["tags"] for case in cases)
    assert any("isolation" in case["tags"] for case in cases)


def test_every_strategy_case_declares_governance_expectations() -> None:
    path = Path(__file__).resolve().parents[2] / "evaluation" / "datasets" / "trajectory.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        case = json.loads(line)
        assert case["agent"] in {"customer_service", "hr_recruiter", "xhs_growth"}
        assert "expected_strategy" in case
        assert case["expected_status"]
        assert isinstance(case["context"], dict)
