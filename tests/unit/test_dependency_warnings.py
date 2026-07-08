import subprocess
import sys
from pathlib import Path

import pytest

from agentkit.config import Settings
from agentkit.runtime import bootstrap


def test_agentkit_and_langgraph_import_without_deprecation_warnings() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            "import agentkit; import langgraph.graph; import langgraph.types",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stderr == ""


def test_agentkit_init_does_not_use_private_langchain_api() -> None:
    source = Path("src/agentkit/__init__.py").read_text(encoding="utf-8")

    assert "langchain_core._api" not in source


def test_build_runtime_validates_settings_before_storage_migrations(monkeypatch, tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        runtime_environment="production",
        approval_checkpointer="memory",
    )
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)

    def unexpected_migration(*_args, **_kwargs):
        raise AssertionError("storage migration must not start before settings validation")

    monkeypatch.setattr(bootstrap, "run_storage_migrations", unexpected_migration)

    with pytest.raises(ValueError, match="durable approval checkpointer"):
        bootstrap.build_runtime(
            tenant_id="company_alpha",
            db_path=tmp_path / "runtime.sqlite",
        )
