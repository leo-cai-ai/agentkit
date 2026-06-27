"""Load golden datasets (.jsonl or .json) into evaluation cases."""

from __future__ import annotations

import json
from pathlib import Path

from .case import EvalCase


def load_cases(path: str | Path) -> list[EvalCase]:
    """Load cases from a ``.jsonl`` file (one case per line) or a ``.json`` list."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".jsonl":
        cases = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                cases.append(EvalCase.from_dict(json.loads(line)))
            except (ValueError, KeyError) as exc:
                raise ValueError(f"{p}:{line_no}: invalid case: {exc}") from exc
        return cases
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("cases", [])
    if not isinstance(data, list):
        raise ValueError(f"{p}: expected a list of cases or {{'cases': [...]}}")
    return [EvalCase.from_dict(item) for item in data]


__all__ = ["load_cases"]
