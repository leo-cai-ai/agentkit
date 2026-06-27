import logging

import pytest

from agentkit.core.logging_config import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Snapshot and restore the root logger so these tests do not leak the
    persistent ``_agentkit`` handler or an elevated level into the session."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_configure_is_idempotent_and_sets_level():
    configure_logging("INFO")
    configure_logging("INFO")  # second call must not add duplicate handlers
    root = logging.getLogger()
    assert len([h for h in root.handlers if getattr(h, "_agentkit", False)]) == 1


def test_get_logger_returns_namespaced_logger():
    logger = get_logger("agentkit.test")
    assert logger.name == "agentkit.test"
