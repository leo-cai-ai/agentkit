"""Backward-compatible entry point. Prefer `agentkit run-demo`."""

from agentkit.cli import _run_demo

if __name__ == "__main__":
    _run_demo()
