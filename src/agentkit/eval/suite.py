"""可版本化的企业评测套件配置。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .case import EvalCase
from .dataset import load_cases


@dataclass(frozen=True)
class EvalFilters:
    include_tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    case_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalExecution:
    repetitions: int = 1
    concurrency: int = 1


@dataclass(frozen=True)
class EvalGates:
    min_pass_rate: float = 1.0
    min_mean_score: float = 0.0
    require_judge: bool = False


@dataclass(frozen=True)
class EvalSuite:
    id: str
    version: str
    target: str
    datasets: tuple[str, ...]
    filters: EvalFilters = EvalFilters()
    execution: EvalExecution = EvalExecution()
    gates: EvalGates = EvalGates()


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("expected a list of strings")
    return tuple(str(item) for item in value)


def _probability(name: str, value: Any) -> float:
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return result


def load_eval_suite(path: str | Path) -> EvalSuite:
    suite_path = Path(path)
    raw = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{suite_path}: suite must be a mapping")
    filters = raw.get("filters") or {}
    execution = raw.get("execution") or {}
    gates = raw.get("gates") or {}
    datasets = _strings(raw.get("datasets"))
    if not datasets:
        raise ValueError("datasets must contain at least one path")
    target = str(raw.get("target") or "")
    if target not in {"llm", "gateway", "gateway-trace"}:
        raise ValueError("target must be llm, gateway, or gateway-trace")
    repetitions = int(execution.get("repetitions", 1))
    concurrency = int(execution.get("concurrency", 1))
    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    suite_id = str(raw.get("id") or "").strip()
    if not suite_id:
        raise ValueError("id is required")
    return EvalSuite(
        id=suite_id,
        version=str(raw.get("version") or "1"),
        target=target,
        datasets=datasets,
        filters=EvalFilters(
            include_tags=_strings(filters.get("include_tags")),
            exclude_tags=_strings(filters.get("exclude_tags")),
            case_ids=_strings(filters.get("case_ids")),
        ),
        execution=EvalExecution(repetitions=repetitions, concurrency=concurrency),
        gates=EvalGates(
            min_pass_rate=_probability("min_pass_rate", gates.get("min_pass_rate", 1.0)),
            min_mean_score=_probability("min_mean_score", gates.get("min_mean_score", 0.0)),
            require_judge=bool(gates.get("require_judge", False)),
        ),
    )


def filter_cases(
    cases: list[EvalCase],
    *,
    include_tags: tuple[str, ...] = (),
    exclude_tags: tuple[str, ...] = (),
    case_ids: tuple[str, ...] = (),
) -> list[EvalCase]:
    include = set(include_tags)
    exclude = set(exclude_tags)
    ids = set(case_ids)
    return [
        case
        for case in cases
        if (not ids or case.id in ids)
        and (not include or bool(include.intersection(case.tags)))
        and not exclude.intersection(case.tags)
    ]


def load_suite_cases(suite: EvalSuite, *, suite_path: str | Path) -> list[EvalCase]:
    base = Path(suite_path).resolve().parent
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for dataset in suite.datasets:
        dataset_path = Path(dataset)
        if not dataset_path.is_absolute():
            dataset_path = base / dataset_path
        for case in load_cases(dataset_path):
            if case.id in seen:
                raise ValueError(f"duplicate case id: {case.id}")
            seen.add(case.id)
            cases.append(case)
    selected = filter_cases(
        cases,
        include_tags=suite.filters.include_tags,
        exclude_tags=suite.filters.exclude_tags,
        case_ids=suite.filters.case_ids,
    )
    if not selected:
        raise ValueError("suite filters selected no cases")
    validate_suite_cases(selected)
    return selected


def validate_suite_cases(cases: list[EvalCase]) -> None:
    empty = [case.id for case in cases if not case.checks]
    if empty:
        raise ValueError(f"cases have no executable checks: {', '.join(empty)}")


def resolve_dataset_paths(suite: EvalSuite, *, suite_path: str | Path) -> tuple[Path, ...]:
    base = Path(suite_path).resolve().parent
    return tuple(
        path if path.is_absolute() else base / path
        for path in (Path(item) for item in suite.datasets)
    )


__all__ = [
    "EvalExecution",
    "EvalFilters",
    "EvalGates",
    "EvalSuite",
    "filter_cases",
    "load_eval_suite",
    "load_suite_cases",
    "resolve_dataset_paths",
    "validate_suite_cases",
]
