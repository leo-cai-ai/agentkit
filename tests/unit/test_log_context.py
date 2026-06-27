"""Unit tests for run_id logging context."""

from __future__ import annotations

import logging

from agentkit.core import log_context
from agentkit.core.logging_config import _RunIdFilter


def test_default_is_dash() -> None:
    assert log_context.current_run_id() == "-"


def test_bind_sets_and_restores() -> None:
    assert log_context.current_run_id() == "-"
    with log_context.bind_run_id("run-123"):
        assert log_context.current_run_id() == "run-123"
    assert log_context.current_run_id() == "-"


def test_set_reset_roundtrip() -> None:
    token = log_context.set_run_id("run-abc")
    assert log_context.current_run_id() == "run-abc"
    log_context.reset_run_id(token)
    assert log_context.current_run_id() == "-"


def test_filter_fills_current_run_id() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    with log_context.bind_run_id("run-filter"):
        _RunIdFilter().filter(record)
    assert record.run_id == "run-filter"


def test_filter_keeps_explicit_run_id() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    record.run_id = "explicit"
    with log_context.bind_run_id("ctx"):
        _RunIdFilter().filter(record)
    assert record.run_id == "explicit"
