"""Persistent workflow artifact stores."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from agentkit.core.artifacts import (
    ArtifactPayloadTooLargeError,
    SqliteArtifactStore,
    build_artifact_store,
)


def _payload_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_sqlite_artifact_store_persists_json_across_fresh_instance(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"
    payload = {"message": "你好", "values": [1, 2, 3]}
    store = build_artifact_store(
        backend="sqlite",
        tenant_id="tenant-a",
        run_id="run-a",
        sqlite_path=db_path,
    )

    written = store.put(
        kind="workflow.result",
        payload=payload,
        summary="Stored result",
        metadata={"step": "result"},
    )

    fresh_store = build_artifact_store(
        backend="sqlite",
        tenant_id="tenant-a",
        run_id="run-a",
        sqlite_path=db_path,
    )
    restored = fresh_store.get(written.artifact_id)

    assert restored.payload == payload
    assert restored.payload_sha256 == _payload_digest(payload)
    assert restored.payload_bytes == len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    assert [record.artifact_id for record in fresh_store.list()] == [written.artifact_id]


def test_sqlite_artifact_store_rejects_payload_above_limit(tmp_path) -> None:
    store = build_artifact_store(
        backend="sqlite",
        tenant_id="tenant-a",
        run_id="run-a",
        sqlite_path=tmp_path / "runtime.sqlite",
        max_payload_bytes=8,
    )

    with pytest.raises(ArtifactPayloadTooLargeError):
        store.put(kind="workflow.result", payload={"value": "too large"})


def test_sqlite_artifact_store_scopes_reads_to_tenant_and_run(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"
    writer = build_artifact_store(
        backend="sqlite",
        tenant_id="tenant-a",
        run_id="run-a",
        sqlite_path=db_path,
    )
    record = writer.put(kind="workflow.result", payload={"ok": True})

    other_tenant = build_artifact_store(
        backend="sqlite",
        tenant_id="tenant-b",
        run_id="run-a",
        sqlite_path=db_path,
    )
    other_run = build_artifact_store(
        backend="sqlite",
        tenant_id="tenant-a",
        run_id="run-b",
        sqlite_path=db_path,
    )

    with pytest.raises(KeyError):
        other_tenant.get(record.artifact_id)
    with pytest.raises(KeyError):
        other_run.get(record.artifact_id)


def test_sqlite_artifact_store_rejects_non_json_payload(tmp_path) -> None:
    store = build_artifact_store(
        backend="sqlite",
        tenant_id="tenant-a",
        run_id="run-a",
        sqlite_path=tmp_path / "runtime.sqlite",
    )

    with pytest.raises(TypeError):
        store.put(kind="workflow.result", payload={"not_json": object()})


def test_memory_artifact_store_preserves_existing_unrestricted_payload_behavior() -> None:
    payload = object()
    store = build_artifact_store(
        backend="memory",
        tenant_id="tenant-a",
        run_id="run-a",
    )

    assert store.put(kind="workflow.result", payload=payload).payload is payload


def test_sqlite_artifact_store_closes_each_operation_connection(tmp_path, monkeypatch) -> None:
    class TrackingConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.closed = False

        def close(self) -> None:
            self.closed = True
            super().close()

    db_path = tmp_path / "runtime.sqlite"
    store = SqliteArtifactStore(
        tenant_id="tenant-a",
        run_id="run-a",
        sqlite_path=db_path,
    )
    opened: list[TrackingConnection] = []

    def tracked_connect() -> TrackingConnection:
        connection = sqlite3.connect(db_path, factory=TrackingConnection)
        connection.row_factory = sqlite3.Row
        opened.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", tracked_connect)

    record = store.put(kind="workflow.result", payload={"ok": True})
    store.get(record.artifact_id)
    store.list()

    assert len(opened) == 3
    assert all(connection.closed for connection in opened)
