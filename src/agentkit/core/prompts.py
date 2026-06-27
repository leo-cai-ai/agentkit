"""Prompt file loading for agent and skill definitions."""

from __future__ import annotations

from pathlib import Path


def load_prompt_files(*, base_dir: Path, prompt_files: dict[str, str]) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for name, relative_path in prompt_files.items():
        path = base_dir / relative_path
        prompts[name] = path.read_text(encoding="utf-8").strip()
    return prompts
