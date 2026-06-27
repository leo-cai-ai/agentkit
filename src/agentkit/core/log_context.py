"""Per-run logging context.

Carries the current ``run_id`` in a context variable so log records emitted
anywhere during a run (LLM client, executor, governance) are automatically
correlated, without threading ``run_id`` through every call site.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_run_id: ContextVar[str] = ContextVar("agentkit_run_id", default="-")


def current_run_id() -> str:
    return _run_id.get()


def set_run_id(run_id: str) -> Token[str]:
    return _run_id.set(run_id or "-")


def reset_run_id(token: Token[str]) -> None:
    _run_id.reset(token)


@contextmanager
def bind_run_id(run_id: str) -> Iterator[None]:
    token = set_run_id(run_id)
    try:
        yield
    finally:
        reset_run_id(token)


__all__ = ["current_run_id", "set_run_id", "reset_run_id", "bind_run_id"]
