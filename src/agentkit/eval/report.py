"""评测运行报告的版本化、持久化与基线比较。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .case import EvalReport

SCHEMA_VERSION = "1.0"


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _dataset_hashes(paths: tuple[str | Path, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in paths:
        path = Path(item)
        result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def load_run_report(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: report must be an object")
    return data


def build_run_report(
    report: EvalReport,
    *,
    suite_id: str,
    suite_version: str,
    target: str,
    min_pass_rate: float,
    min_mean_score: float,
    dataset_paths: tuple[str | Path, ...],
    tenant_id: str = "",
    provider: str = "",
    model: str = "",
    context_manifest_hash: str = "",
    repetitions: int = 1,
    concurrency: int = 1,
    baseline_path: str | Path | None = None,
) -> dict[str, Any]:
    summary = report.summary()
    passed = report.gate(
        min_pass_rate=min_pass_rate,
        min_mean_score=min_mean_score,
    )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "suite": {"id": suite_id, "version": suite_version},
        "target": target,
        "environment": {
            "tenant_id": tenant_id,
            "provider": provider,
            "model": model,
            "git_commit": _git_commit(),
            "context_manifest_hash": context_manifest_hash,
        },
        "datasets": _dataset_hashes(dataset_paths),
        "execution": {"repetitions": repetitions, "concurrency": concurrency},
        "gate": {
            "passed": passed,
            "min_pass_rate": min_pass_rate,
            "min_mean_score": min_mean_score,
        },
        "summary": summary,
        "results": [result.to_dict() for result in report.results],
    }
    if baseline_path is not None:
        baseline = load_run_report(baseline_path)
        baseline_summary = baseline.get("summary", {})
        payload["baseline_diff"] = {
            "baseline_path": str(baseline_path),
            "pass_rate_delta": round(
                float(summary["pass_rate"]) - float(baseline_summary.get("pass_rate", 0.0)),
                3,
            ),
            "mean_score_delta": round(
                float(summary["mean_score"]) - float(baseline_summary.get("mean_score", 0.0)),
                3,
            ),
        }
    return payload


def write_run_report(report: dict[str, Any], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "SCHEMA_VERSION",
    "build_run_report",
    "load_run_report",
    "write_run_report",
]
