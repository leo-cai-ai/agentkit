"""Unit tests for the PostgreSQL connection layer and pgvector wiring.

These do not require a live database; they cover DSN construction, the missing
driver error, and lazy backend selection (no connection on construction).
"""

from __future__ import annotations

import sys

import pytest

from agentkit.config import Settings
from agentkit.core import pg


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_build_dsn_prefers_explicit_dsn() -> None:
    dsn = pg.build_dsn(_settings(pg_dsn="postgresql://u:p@h:5432/db"))
    assert dsn == "postgresql://u:p@h:5432/db"


def test_build_dsn_assembles_parts() -> None:
    dsn = pg.build_dsn(
        _settings(
            pg_host="db.internal",
            pg_port=6543,
            pg_database="mem",
            pg_user="agent",
            pg_sslmode="require",
        )
    )
    assert "host=db.internal" in dsn
    assert "port=6543" in dsn
    assert "dbname=mem" in dsn
    assert "user=agent" in dsn
    assert "sslmode=require" in dsn
    # No password configured -> not present in the DSN.
    assert "password" not in dsn


def test_build_dsn_escapes_password_with_spaces() -> None:
    dsn = pg.build_dsn(_settings(pg_password="p ass'word"))
    assert "password='p ass\\'word'" in dsn


def test_require_psycopg_missing_raises_install_hint(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)  # force ImportError on import
    with pytest.raises(RuntimeError, match=r"agentkit\[pg\]"):
        pg.require_psycopg()


def test_pg_available_returns_bool() -> None:
    assert isinstance(pg.pg_available(), bool)


def test_build_vector_store_postgres_is_lazy() -> None:
    from agentkit.core.memory.pg_vector_store import PgVectorStore
    from agentkit.core.memory.vector_store import build_vector_store

    # Selecting the postgres backend must not connect (schema is lazy).
    cfg = _settings(vector_store_backend="postgres")
    store = build_vector_store(cfg, store=None)  # type: ignore[arg-type]
    assert isinstance(store, PgVectorStore)


def test_build_vector_store_unknown_backend_rejected() -> None:
    from agentkit.core.memory.vector_store import build_vector_store

    class _Cfg:
        vector_store_backend = "milvus"

    with pytest.raises(ValueError, match="Unsupported vector_store_backend"):
        build_vector_store(_Cfg(), store=None)  # type: ignore[arg-type]
