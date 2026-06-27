"""Centralised, idempotent logging setup for the runtime."""

from __future__ import annotations

import logging

from .log_context import current_run_id

_FORMAT = "%(asctime)s %(levelname)s %(name)s [run_id=%(run_id)s] %(message)s"


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = current_run_id()
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_agentkit", False):
            root.setLevel(level)
            return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_RunIdFilter())
    handler._agentkit = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
