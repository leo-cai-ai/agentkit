import subprocess
import sys


def test_langgraph_allowed_objects_warning_is_suppressed() -> None:
    completed = subprocess.run(
        [sys.executable, "-W", "default", "-c", "import agentkit; import langgraph.graph"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "allowed_objects" not in completed.stderr
