import subprocess
import sys
from pathlib import Path


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
