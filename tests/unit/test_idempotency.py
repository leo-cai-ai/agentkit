"""Durable tool idempotency ledger behavior."""

from __future__ import annotations

import hashlib

import pytest

from agentkit.core.artifacts import canonical_json
from agentkit.core.idempotency import (
    IdempotencyConflictError,
    IdempotencyError,
    IdempotencyInProgressError,
    IdempotencyOutcomeUnknownError,
    PostgresIdempotencyStore,
    SqliteIdempotencyStore,
    build_idempotency_store,
    canonical_args_hash,
    key_digest,
)


def _store(tmp_path):
    return build_idempotency_store(
        backend="sqlite",
        tenant_id="tenant-a",
        sqlite_path=tmp_path / "runtime.sqlite",
    )


def test_stores_expose_their_tenant_id(tmp_path, monkeypatch) -> None:
    sqlite_store = SqliteIdempotencyStore(
        tenant_id="tenant-a",
        sqlite_path=tmp_path / "runtime.sqlite",
    )
    monkeypatch.setattr(PostgresIdempotencyStore, "_init_schema", lambda self: None)
    postgres_store = PostgresIdempotencyStore(tenant_id="tenant-b", settings=object())

    assert sqlite_store.tenant_id == "tenant-a"
    assert postgres_store.tenant_id == "tenant-b"


def test_success_is_reused_after_store_recreation(tmp_path) -> None:
    first = _store(tmp_path)
    claim = first.begin(
        tool_name="crm.create",
        idempotency_key="key-1",
        args={"name": "Ada"},
    )
    assert claim.claimed is True
    first.finish_success(claim, {"id": "crm-1"})

    cached = _store(tmp_path).begin(
        tool_name="crm.create",
        idempotency_key="key-1",
        args={"name": "Ada"},
    )

    assert cached.claimed is False
    assert cached.status == "succeeded"
    assert cached.result == {"id": "crm-1"}


def test_different_args_conflict_even_after_unknown_outcome(tmp_path) -> None:
    store = _store(tmp_path)
    claim = store.begin(
        tool_name="crm.create",
        idempotency_key="key-1",
        args={"name": "Ada"},
    )
    store.finish_unknown(claim, "response confirmation timed out")

    with pytest.raises(IdempotencyConflictError):
        store.begin(
            tool_name="crm.create",
            idempotency_key="key-1",
            args={"name": "Grace"},
        )


def test_duplicate_running_claim_raises_in_progress(tmp_path) -> None:
    store = _store(tmp_path)
    store.begin(tool_name="crm.create", idempotency_key="key-1", args={})

    with pytest.raises(IdempotencyInProgressError):
        store.begin(tool_name="crm.create", idempotency_key="key-1", args={})


def test_duplicate_unknown_claim_raises_outcome_unknown(tmp_path) -> None:
    store = _store(tmp_path)
    claim = store.begin(tool_name="crm.create", idempotency_key="key-1", args={})
    store.finish_unknown(claim, "response confirmation timed out")

    with pytest.raises(IdempotencyOutcomeUnknownError):
        store.begin(tool_name="crm.create", idempotency_key="key-1", args={})


def test_duplicate_failed_claim_raises_typed_failed_error(tmp_path) -> None:
    store = _store(tmp_path)
    claim = store.begin(tool_name="crm.create", idempotency_key="key-1", args={})
    store.finish_failure(claim, "request rejected")

    with pytest.raises(IdempotencyError) as failure:
        store.begin(tool_name="crm.create", idempotency_key="key-1", args={})

    assert type(failure.value).__name__ == "IdempotencyFailedError"


@pytest.mark.parametrize(
    ("finish", "args"),
    [
        ("finish_success", ({"id": "crm-1"},)),
        ("finish_failure", ("request rejected before submission",)),
        ("finish_unknown", ("response confirmation timed out",)),
    ],
)
def test_finish_methods_cannot_update_non_running_claim(tmp_path, finish, args) -> None:
    store = _store(tmp_path)
    claim = store.begin(tool_name="crm.create", idempotency_key="key-1", args={})
    store.finish_success(claim, {"id": "crm-1"})

    with pytest.raises(IdempotencyError):
        getattr(store, finish)(claim, *args)


def test_canonical_args_hash_excludes_key_and_key_digest_hides_raw_input() -> None:
    args = {"name": "Ada", "_idempotency_key": "raw idempotency secret"}

    assert canonical_args_hash(args) == canonical_args_hash(
        {"name": "Ada", "_idempotency_key": "another key"}
    )
    assert canonical_args_hash(args) == hashlib.sha256(
        canonical_json({"name": "Ada"})
    ).hexdigest()

    digest = key_digest("raw idempotency secret")
    assert digest == hashlib.sha256(b"raw idempotency secret").hexdigest()[:16]
    assert "raw idempotency secret" not in digest


def test_factory_rejects_unsupported_backend(tmp_path) -> None:
    with pytest.raises(ValueError, match="Unsupported idempotency backend"):
        build_idempotency_store(
            backend="memory",
            tenant_id="tenant-a",
            sqlite_path=tmp_path / "runtime.sqlite",
        )


def test_postgres_existing_record_is_classified_while_row_lock_is_held(monkeypatch) -> None:
    class GuardedRow:
        def __init__(self, connection) -> None:
            self._connection = connection

        def __getitem__(self, index: int):
            assert self._connection.closed is False
            return (
                canonical_args_hash({"name": "Ada"}),
                "succeeded",
                '{"id":"crm-1"}',
            )[index]

    class Cursor:
        def __init__(self, *, rowcount: int, row=None) -> None:
            self.rowcount = rowcount
            self._row = row

        def fetchone(self):
            return self._row

    class Connection:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, query: str, params):
            if "INSERT INTO" in query:
                return Cursor(rowcount=0)
            assert "FOR UPDATE" in query
            return Cursor(rowcount=0, row=GuardedRow(self))

    class ConnectionContext:
        def __init__(self) -> None:
            self.connection = Connection()

        def __enter__(self):
            return self.connection

        def __exit__(self, *args) -> None:
            self.connection.closed = True

    monkeypatch.setattr(PostgresIdempotencyStore, "_init_schema", lambda self: None)
    store = PostgresIdempotencyStore(tenant_id="tenant-a", settings=object())
    monkeypatch.setattr(store, "_connect", ConnectionContext)

    cached = store.begin(
        tool_name="crm.create",
        idempotency_key="key-1",
        args={"name": "Ada"},
    )

    assert cached.result == {"id": "crm-1"}
