"""仓库目录职责与评估数据集门禁。"""

from pathlib import Path

from agentkit.eval.dataset import load_cases


REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_ROOT = REPO_ROOT / "evaluation" / "datasets"


def test_standard_evaluation_dataset_uses_explicit_dataset_root() -> None:
    assert not (REPO_ROOT / "evals").exists()
    cases = load_cases(DATASET_ROOT / "golden.jsonl")
    assert cases


def test_trajectory_dataset_exists_in_explicit_dataset_root() -> None:
    assert (DATASET_ROOT / "trajectory.jsonl").is_file()
