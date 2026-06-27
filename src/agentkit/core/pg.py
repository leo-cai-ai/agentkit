"""PostgreSQL connection helpers (optional ``[pg]`` extra).

Centralises DSN construction and connection lifecycle so any PG-backed store
(pgvector memory today; conversation/audit later) shares one configuration
surface. ``psycopg`` is imported lazily so the package stays importable — and
all non-PG backends keep working — when the driver is not installed.

Configuration (env, ``AGENTKIT_`` prefix):

- ``AGENTKIT_PG_DSN``        full libpq DSN or URL (takes precedence if set)
- ``AGENTKIT_PG_HOST``       default ``localhost``
- ``AGENTKIT_PG_PORT``       default ``5432``
- ``AGENTKIT_PG_DATABASE``   default ``agentkit``
- ``AGENTKIT_PG_USER``       default ``agentkit``
- ``AGENTKIT_PG_PASSWORD``   secret (omitted from the DSN if unset)
- ``AGENTKIT_PG_SSLMODE``    default ``prefer``
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


def require_psycopg() -> Any:
    """Return the ``psycopg`` (v3) module or raise a clear install hint."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "PostgreSQL backend requires the 'psycopg' driver. "
            "Install it with: pip install 'agentkit[pg]'"
        ) from exc
    return psycopg


def pg_available() -> bool:
    """True when the psycopg driver is importable."""
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return True


def _escape(value: str) -> str:
    """Escape a value for a libpq key=value DSN (quote when needed)."""
    text = str(value)
    if text == "" or any(ch in text for ch in " '\\"):
        return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return text


def build_dsn(settings: Any = None) -> str:
    """Build a libpq connection string from settings (DSN wins if provided)."""
    if settings is None:
        from agentkit.config import get_settings

        settings = get_settings()

    dsn = getattr(settings, "pg_dsn", None)
    if dsn:
        return str(dsn)

    password = getattr(settings, "pg_password", None)
    pw = password.get_secret_value() if password is not None else None

    parts: list[tuple[str, str | None]] = [
        ("host", getattr(settings, "pg_host", "localhost")),
        ("port", str(getattr(settings, "pg_port", 5432))),
        ("dbname", getattr(settings, "pg_database", "agentkit")),
        ("user", getattr(settings, "pg_user", "agentkit")),
        ("password", pw),
        ("sslmode", getattr(settings, "pg_sslmode", "prefer")),
    ]
    return " ".join(
        f"{key}={_escape(value)}" for key, value in parts if value is not None and value != ""
    )


@contextmanager
def connection(settings: Any = None) -> Iterator[Any]:
    """Open a short-lived connection, commit on success, rollback on error.

    Short-lived connections are intentionally simple and thread-safe under the
    Flask worker pool. Swap in ``psycopg_pool`` here if call volume warrants it.
    """
    psycopg = require_psycopg()
    conn = psycopg.connect(build_dsn(settings))
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


__all__ = ["require_psycopg", "pg_available", "build_dsn", "connection"]
