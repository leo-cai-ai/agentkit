# Durable Execution Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Persist workflow artifacts and tool idempotency decisions across runtime restarts using the current SQLite and PostgreSQL backends.

**Architecture:** A migration runner owns runtime schema versions. Artifact and idempotency protocols have SQLite/PostgreSQL implementations. Runtime bootstrap injects them into PlanExecutor and ToolExecutor while preserving in-memory defaults for direct unit construction.

**Tech Stack:** Python 3.11+, SQLite, PostgreSQL via psycopg, pytest, LangGraph.

---

## File structure

- Create: src/agentkit/core/migrations.py — runtime schema migration runner.
- Create: src/agentkit/core/idempotency.py — idempotency contracts and storage adapters.
- Create: tests/unit/test_migrations.py — SQLite migration contract.
- Create: tests/unit/test_persistent_artifacts.py — artifact persistence contract.
- Create: tests/unit/test_idempotency.py — idempotency state-machine contract.
- Create: tests/integration/test_durable_execution.py — runtime wiring and optional PostgreSQL contract.
- Modify: src/agentkit/core/artifacts.py — canonical JSON and persistent stores.
- Modify: src/agentkit/core/audit.py — call migrations instead of inline DDL.
- Modify: src/agentkit/core/tool_executor.py — durable keyed-tool guard.
- Modify: src/agentkit/core/executor.py — persistent artifact factory and ledger injection.
- Modify: src/agentkit/core/gateway.py and src/agentkit/runtime/bootstrap.py — construct and inject stores.
- Modify: src/agentkit/cli.py and src/agentkit/config.py — startup migration and payload-size setting.
- Modify: tests/unit/test_tool_executor.py, tests/unit/test_workflow_artifacts.py, tests/unit/test_config.py, README.md, docs/ARCHITECTURE.md, docs/DEPLOYMENT.md.

### Task 1: Version the runtime schema

**Files:**
- Create: tests/unit/test_migrations.py
- Create: src/agentkit/core/migrations.py
- Modify: src/agentkit/core/audit.py

- [ ] **Step 1: Write failing SQLite migration tests**

~~~python
from agentkit.core.migrations import run_sqlite_migrations


def test_sqlite_migrations_create_runtime_tables_and_are_idempotent(tmp_path) -> None:
    path = tmp_path / "runtime.sqlite"
    assert run_sqlite_migrations(path) == [1]
    assert run_sqlite_migrations(path) == []

    import sqlite3
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        versions = conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()

    assert {
        "schema_migrations",
        "task_runs",
        "audit_events",
        "workflow_artifacts",
        "tool_idempotency_records",
    } <= tables
    assert versions == [(1,)]


def test_sqlite_migrations_adopt_existing_audit_tables(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite"
    import sqlite3
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE task_runs (run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
            "user_id TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL, "
            "started_at REAL NOT NULL, finished_at REAL)"
        )
        conn.execute(
            "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_id TEXT NOT NULL, ts REAL NOT NULL, event_type TEXT NOT NULL, "
            "payload_json TEXT NOT NULL)"
        )

    assert run_sqlite_migrations(path) == [1]
~~~

- [ ] **Step 2: Run the test to verify it fails**

Run: python -m pytest tests/unit/test_migrations.py -v

Expected: collection fails because agentkit.core.migrations does not exist.

- [ ] **Step 3: Implement the migration module**

Create a migration list with version 1. The module exports:

~~~python
def run_sqlite_migrations(path: str | Path) -> list[int]: ...
def run_postgres_migrations(settings: Any) -> list[int]: ...
def run_storage_migrations(
    settings: Any,
    *,
    sqlite_path: Path | None = None,
) -> list[int]: ...
~~~

Version 1 creates schema_migrations, preserves the current task_runs and audit_events DDL/indexes, and creates these tables:

~~~sql
CREATE TABLE IF NOT EXISTS workflow_artifacts (
    artifact_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_bytes INTEGER NOT NULL,
    summary TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_artifacts_scope
ON workflow_artifacts(tenant_id, run_id, created_at, artifact_id);

CREATE TABLE IF NOT EXISTS tool_idempotency_records (
    tenant_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    args_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    error_message TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (tenant_id, tool_name, idempotency_key)
);
~~~

The PostgreSQL migration uses JSONB for payload_json, metadata_json, and result_json; DOUBLE PRECISION for timestamps; and the same primary/index keys. SQLite begins each version in one transaction. PostgreSQL applies each version in one connection transaction. Neither new table has a foreign key to task_runs because existing direct tool tests use unregistered run ids.

Replace SQLiteAuditLog._init_schema and PostgresAuditLog._init_schema with the appropriate migration call; existing constructors must still bootstrap an empty database.

- [ ] **Step 4: Run migration and audit regression tests**

Run: python -m pytest tests/unit/test_migrations.py tests/unit/test_pg.py tests/unit/test_metrics.py -v

Expected: PASS. The second migration call returns an empty list and existing audit storage behavior remains unchanged.

- [ ] **Step 5: Commit**

~~~bash
git add src/agentkit/core/migrations.py src/agentkit/core/audit.py tests/unit/test_migrations.py
git commit -m "feat: add versioned runtime storage migrations"
~~~

### Task 2: Add persistent JSON artifact stores

**Files:**
- Create: tests/unit/test_persistent_artifacts.py
- Modify: src/agentkit/core/artifacts.py
- Modify: src/agentkit/config.py
- Modify: tests/unit/test_workflow_artifacts.py
- Modify: tests/unit/test_config.py

- [ ] **Step 1: Write failing artifact persistence tests**

~~~python
import pytest

from agentkit.core.artifacts import (
    ArtifactPayloadTooLargeError,
    build_artifact_store,
)


def test_sqlite_artifacts_survive_store_recreation(tmp_path) -> None:
    first = build_artifact_store(
        backend="sqlite",
        tenant_id="tenant-a",
        run_id="run-a",
        sqlite_path=tmp_path / "runtime.sqlite",
        max_payload_bytes=1024,
    )
    record = first.put(
        kind="report",
        payload={"ranked": ["C-100"]},
        summary="ranked",
        metadata={"step": "rank"},
    )

    second = build_artifact_store(
        backend="sqlite",
        tenant_id="tenant-a",
        run_id="run-a",
        sqlite_path=tmp_path / "runtime.sqlite",
        max_payload_bytes=1024,
    )
    assert second.get(record.artifact_id).payload == {"ranked": ["C-100"]}
    assert second.list()[0].payload_sha256 == record.payload_sha256


def test_sqlite_artifacts_reject_payloads_above_limit(tmp_path) -> None:
    store = build_artifact_store(
        backend="sqlite",
        tenant_id="tenant-a",
        run_id="run-a",
        sqlite_path=tmp_path / "runtime.sqlite",
        max_payload_bytes=8,
    )
    with pytest.raises(ArtifactPayloadTooLargeError):
        store.put(kind="report", payload={"text": "too large"})
~~~

- [ ] **Step 2: Run the tests to verify imports fail**

Run: python -m pytest tests/unit/test_persistent_artifacts.py -v

Expected: collection fails because the new factory and exception are absent.

- [ ] **Step 3: Implement artifact contracts and backends**

Add ArtifactPayloadTooLargeError and canonical_json:

~~~python
def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
~~~

Extend ArtifactRecord with payload_sha256 and payload_bytes. Keep payload out of ref(); ref() adds only payload_sha256 and payload_bytes. Update the existing exact ref assertion.

Implement SqliteArtifactStore and PostgresArtifactStore with constructor fields tenant_id, run_id, max_payload_bytes, and optional on_write. Each put operation serializes before opening a write transaction, rejects a payload above the limit, computes SHA-256, inserts it, returns an ArtifactRecord, and invokes on_write only after insertion. get and list filter by both tenant_id and run_id.

Add this factory:

~~~python
def build_artifact_store(
    *,
    backend: str,
    tenant_id: str,
    run_id: str,
    sqlite_path: Path | None = None,
    settings: Any = None,
    max_payload_bytes: int = 1_048_576,
    on_write: Callable[[ArtifactRecord], None] | None = None,
) -> ArtifactStore:
    ...
~~~

memory returns InMemoryArtifactStore; sqlite requires sqlite_path; postgres requires settings; an unsupported backend raises ValueError.

Add artifact_max_payload_bytes: int = Field(default=1_048_576, gt=0) to Settings. Add its environment variable to test_config._fresh_settings and assert both default and override values.

- [ ] **Step 4: Run artifact/configuration tests**

Run: python -m pytest tests/unit/test_persistent_artifacts.py tests/unit/test_workflow_artifacts.py tests/unit/test_config.py -v

Expected: PASS. Existing in-memory workflow tests pass and a fresh SQLite store reads the earlier artifact.

- [ ] **Step 5: Commit**

~~~bash
git add src/agentkit/core/artifacts.py src/agentkit/config.py tests/unit/test_persistent_artifacts.py tests/unit/test_workflow_artifacts.py tests/unit/test_config.py
git commit -m "feat: persist workflow artifacts by storage backend"
~~~

### Task 3: Add the durable idempotency state machine

**Files:**
- Create: src/agentkit/core/idempotency.py
- Create: tests/unit/test_idempotency.py

- [ ] **Step 1: Write failing state-machine tests**

~~~python
import pytest

from agentkit.core.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyOutcomeUnknownError,
    build_idempotency_store,
)


def test_success_is_reused_after_store_recreation(tmp_path) -> None:
    first = build_idempotency_store(
        backend="sqlite", tenant_id="t", sqlite_path=tmp_path / "runtime.sqlite"
    )
    claim = first.begin(
        tool_name="crm.create",
        idempotency_key="key-1",
        args={"name": "Ada"},
    )
    assert claim.claimed is True
    first.finish_success(claim, {"id": "crm-1"})

    second = build_idempotency_store(
        backend="sqlite", tenant_id="t", sqlite_path=tmp_path / "runtime.sqlite"
    )
    cached = second.begin(
        tool_name="crm.create",
        idempotency_key="key-1",
        args={"name": "Ada"},
    )
    assert cached.claimed is False
    assert cached.result == {"id": "crm-1"}


def test_conflict_running_and_unknown_are_not_reclaimed(tmp_path) -> None:
    store = build_idempotency_store(
        backend="sqlite", tenant_id="t", sqlite_path=tmp_path / "runtime.sqlite"
    )
    claim = store.begin(tool_name="crm.create", idempotency_key="key-1", args={})
    with pytest.raises(IdempotencyInProgressError):
        store.begin(tool_name="crm.create", idempotency_key="key-1", args={})
    store.finish_unknown(claim, "timeout")
    with pytest.raises(IdempotencyOutcomeUnknownError):
        store.begin(tool_name="crm.create", idempotency_key="key-1", args={})
    with pytest.raises(IdempotencyConflictError):
        store.begin(
            tool_name="crm.create",
            idempotency_key="key-1",
            args={"different": True},
        )
~~~

- [ ] **Step 2: Run the tests to verify the missing-module failure**

Run: python -m pytest tests/unit/test_idempotency.py -v

Expected: collection fails because agentkit.core.idempotency does not exist.

- [ ] **Step 3: Implement stores and typed outcomes**

Define IdempotencyError, IdempotencyConflictError, IdempotencyInProgressError, and IdempotencyOutcomeUnknownError. Define IdempotencyClaim with tenant_id, tool_name, idempotency_key, args_sha256, status, and optional result; claimed is true only for status claimed.

canonical_args_hash removes _idempotency_key and hashes canonical_json(args). key_digest returns the first 16 hex characters of SHA-256(key UTF-8).

The protocol exposes begin, finish_success, finish_failure, and finish_unknown. SQLite begin uses BEGIN IMMEDIATE. PostgreSQL begin uses INSERT ... ON CONFLICT DO NOTHING and SELECT ... FOR UPDATE. On an existing key, first compare args_sha256, then return success or raise the exact state exception. finish methods only update a running record matching the claim; a zero-row update raises IdempotencyError. Store errors/results as JSON or text, but never expose a raw idempotency key through the public audit payload.

- [ ] **Step 4: Run ledger and migration tests**

Run: python -m pytest tests/unit/test_idempotency.py tests/unit/test_migrations.py -v

Expected: PASS. A success survives store recreation; running, unknown, and conflicting keys never trigger a new claim.

- [ ] **Step 5: Commit**

~~~bash
git add src/agentkit/core/idempotency.py tests/unit/test_idempotency.py
git commit -m "feat: add durable tool idempotency ledger"
~~~

### Task 4: Integrate the ledger with ToolExecutor

**Files:**
- Modify: src/agentkit/core/tool_executor.py
- Modify: tests/unit/test_tool_executor.py

- [ ] **Step 1: Add failing executor integration tests**

~~~python
def test_durable_idempotency_reuses_result_across_executors(tmp_path) -> None:
    store = build_idempotency_store(
        backend="sqlite", tenant_id="t", sqlite_path=tmp_path / "runtime.sqlite"
    )
    calls = {"count": 0}

    def mutate(_: dict) -> dict:
        calls["count"] += 1
        return {"count": calls["count"]}

    first = ToolExecutor(tenant_id="t", idempotency_store=store)
    assert first.call(
        _tool(mutate, name="crm.create"),
        {"_idempotency_key": "stable"},
    ) == {"count": 1}
    second = ToolExecutor(tenant_id="t", idempotency_store=store)
    assert second.call(
        _tool(mutate, name="crm.create"),
        {"_idempotency_key": "stable"},
    ) == {"count": 1}
    assert calls["count"] == 1


def test_timeout_marks_key_unknown_and_blocks_second_attempt(tmp_path) -> None:
    store = build_idempotency_store(
        backend="sqlite", tenant_id="t", sqlite_path=tmp_path / "runtime.sqlite"
    )
    executor = ToolExecutor(
        tenant_id="t", idempotency_store=store, timeout_seconds=0.01
    )
    with pytest.raises(ToolTimeoutError):
        executor.call(
            _tool(lambda _: time.sleep(0.1) or {}, name="crm.create"),
            {"_idempotency_key": "stable"},
        )
    with pytest.raises(IdempotencyOutcomeUnknownError):
        ToolExecutor(tenant_id="t", idempotency_store=store).call(
            _tool(lambda _: {}, name="crm.create"),
            {"_idempotency_key": "stable"},
        )
~~~

- [ ] **Step 2: Run the tests to verify the new constructor argument is unsupported**

Run: python -m pytest tests/unit/test_tool_executor.py::test_durable_idempotency_reuses_result_across_executors tests/unit/test_tool_executor.py::test_timeout_marks_key_unknown_and_blocks_second_attempt -v

Expected: FAIL with TypeError because ToolExecutor has no idempotency_store argument.

- [ ] **Step 3: Add persistent key handling**

Add idempotency_store: IdempotencyStore | None = None to ToolExecutor. When it is absent, preserve the existing run-local cache exactly. When it is present and args includes _idempotency_key:

~~~python
claim = self._idempotency_store.begin(
    tool_name=tool.name,
    idempotency_key=str(idem_key),
    args=args,
)
if not claim.claimed:
    self._record(
        run_id,
        "idempotency_cache_hit",
        {"tool": tool.name, "key_digest": key_digest(str(idem_key))},
    )
    assert claim.result is not None
    return claim.result
self._record(
    run_id,
    "idempotency_claimed",
    {"tool": tool.name, "key_digest": key_digest(str(idem_key))},
)
~~~

Record finish_success after the final handler result. Before re-raising ToolTimeoutError, record finish_unknown. Before re-raising any other final ToolExecutionError, record finish_failure. Catch typed begin exceptions only to write idempotency_conflict, idempotency_in_progress, or idempotency_outcome_unknown with tool and key_digest, then re-raise. Do not change retry logic or the audit shape of unkeyed calls.

- [ ] **Step 4: Run complete tool executor coverage**

Run: python -m pytest tests/unit/test_tool_executor.py -v

Expected: PASS. Existing run-local caching still passes, and durable cache reuse prevents a second handler call.

- [ ] **Step 5: Commit**

~~~bash
git add src/agentkit/core/tool_executor.py tests/unit/test_tool_executor.py
git commit -m "feat: persist tool idempotency outcomes"
~~~

### Task 5: Inject stores through the live runtime

**Files:**
- Modify: src/agentkit/core/executor.py
- Modify: src/agentkit/core/gateway.py
- Modify: src/agentkit/runtime/bootstrap.py
- Create: tests/integration/test_durable_execution.py

- [ ] **Step 1: Write failing bootstrap wiring tests**

~~~python
def test_runtime_artifact_factory_persists_to_tenant_database(monkeypatch, tmp_path) -> None:
    runtime = build_runtime(db_path=tmp_path / "runtime.sqlite")
    store = build_artifact_store(
        backend="sqlite",
        tenant_id="AI-ABC",
        run_id="run-1",
        sqlite_path=runtime.db_path,
    )
    record = store.put(kind="test", payload={"ok": True})
    restored = build_artifact_store(
        backend="sqlite",
        tenant_id="AI-ABC",
        run_id="run-1",
        sqlite_path=runtime.db_path,
    ).get(record.artifact_id)
    assert restored.payload == {"ok": True}
~~~

Add a second test which creates two PlanExecutor instances with one injected SQLite idempotency store, stubs the execution brief helper, and asserts a keyed mutation is called once.

- [ ] **Step 2: Run the integration test to demonstrate missing runtime injection**

Run: python -m pytest tests/integration/test_durable_execution.py -v

Expected: FAIL because PlanExecutor and AgentGateway do not accept the new dependencies.

- [ ] **Step 3: Thread factories and stores through constructors**

Add optional constructor parameters to AgentGateway and PlanExecutor:

~~~python
artifact_store_factory: Callable[[str], ArtifactStore] | None = None
idempotency_store: IdempotencyStore | None = None
~~~

In PlanExecutor.execute, replace the unconditional InMemoryArtifactStore with the injected factory and retain the in-memory on_write fallback. Pass idempotency_store to every ToolExecutor built for normal and deferred actions.

In bootstrap, call run_storage_migrations before constructing audit storage. Build one idempotency store for the tenant. Build an artifact_store_factory closure that captures settings, db_path, tenant id, maximum payload bytes, and an audit callback. Pass both into AgentGateway.

Keep artifact_written for compatibility and additionally emit artifact_persisted with artifact_id, kind, payload_sha256, payload_bytes, and backend only.

- [ ] **Step 4: Run runtime and existing approval regressions**

Run: python -m pytest tests/integration/test_durable_execution.py tests/integration/test_approval_resume.py tests/integration/test_xhs_publish_approval.py -v

Expected: PASS. Artifacts survive a new store, keyed mutations are not repeated, and approval/XHS paths remain unchanged.

- [ ] **Step 5: Commit**

~~~bash
git add src/agentkit/core/executor.py src/agentkit/core/gateway.py src/agentkit/runtime/bootstrap.py tests/integration/test_durable_execution.py
git commit -m "feat: wire durable execution stores into runtime"
~~~

### Task 6: Make migrations operable and document the contract

**Files:**
- Modify: src/agentkit/cli.py
- Modify: tests/unit/test_cli.py
- Modify: README.md
- Modify: docs/ARCHITECTURE.md
- Modify: docs/DEPLOYMENT.md

- [ ] **Step 1: Write a failing init-db migration test**

~~~python
def test_init_db_runs_sqlite_runtime_migrations(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: Settings(_env_file=None, storage_backend="sqlite"),
    )
    assert cli._init_db() == 0
    assert "runtime migrations ready" in capsys.readouterr().out
~~~

- [ ] **Step 2: Run the focused test**

Run: python -m pytest tests/unit/test_cli.py::test_init_db_runs_sqlite_runtime_migrations -v

Expected: FAIL because init-db does not call the migration runner for SQLite.

- [ ] **Step 3: Invoke migrations before readiness checks and document behavior**

In cli._init_db, resolve the selected tenant, pass DATA_DIR / f"{tenant_id}.sqlite" to run_storage_migrations, and print:

~~~python
print(f"[ok] runtime migrations ready: {applied or 'up-to-date'}")
~~~

For PostgreSQL, run migrations after connectivity validation and before audit/conversation checks. Update the three docs to state that workflow_artifacts and tool_idempotency_records are durable tables; idempotency scope is tenant/tool/key; payload conflicts are rejected; keyed timeouts can become outcome_unknown; artifacts are JSON and limited by AGENTKIT_ARTIFACT_MAX_PAYLOAD_BYTES default 1048576; init-db/runtime startup applies schema migrations.

- [ ] **Step 4: Run focused final tests**

Run: python -m pytest tests/unit/test_cli.py tests/unit/test_migrations.py tests/unit/test_persistent_artifacts.py tests/unit/test_idempotency.py tests/unit/test_tool_executor.py -v

Expected: PASS.

- [ ] **Step 5: Commit**

~~~bash
git add src/agentkit/cli.py tests/unit/test_cli.py README.md docs/ARCHITECTURE.md docs/DEPLOYMENT.md
git commit -m "docs: document durable execution storage"
~~~

### Task 7: Verify the feature across the whole repository

**Files:**
- Modify: tests/integration/test_durable_execution.py

- [ ] **Step 1: Add optional PostgreSQL contract coverage**

~~~python
@pytest.mark.skipif(
    not os.environ.get("AGENTKIT_TEST_PG_DSN"),
    reason="requires PostgreSQL contract database",
)
def test_postgres_artifact_and_idempotency_contract() -> None:
    settings = Settings(
        _env_file=None,
        storage_backend="postgres",
        pg_dsn=os.environ["AGENTKIT_TEST_PG_DSN"],
    )
    run_postgres_migrations(settings)
    artifacts = build_artifact_store(
        backend="postgres",
        tenant_id="contract",
        run_id="artifact-run",
        settings=settings,
    )
    record = artifacts.put(kind="contract", payload={"value": 1})
    assert artifacts.get(record.artifact_id).payload == {"value": 1}

    ledger = build_idempotency_store(
        backend="postgres", tenant_id="contract", settings=settings
    )
    claim = ledger.begin(
        tool_name="contract.write",
        idempotency_key="contract-key",
        args={"value": 1},
    )
    ledger.finish_success(claim, {"written": True})
    assert ledger.begin(
        tool_name="contract.write",
        idempotency_key="contract-key",
        args={"value": 1},
    ).result == {"written": True}
~~~

- [ ] **Step 2: Run all local tests and lint**

Run: python -m pytest

Expected: PASS, with the PostgreSQL contract skipped unless AGENTKIT_TEST_PG_DSN is configured.

Run: python -m ruff check .

Expected: All checks passed.

- [ ] **Step 3: Run PostgreSQL contract when a disposable database is configured**

Run: $env:AGENTKIT_TEST_PG_DSN='postgresql://...'; python -m pytest tests/integration/test_durable_execution.py::test_postgres_artifact_and_idempotency_contract -v

Expected: PASS.

- [ ] **Step 4: Commit**

~~~bash
git add tests/integration/test_durable_execution.py
git commit -m "test: cover durable execution storage contracts"
~~~

## Plan self-review

- Spec coverage: Task 1 adds versioned schema ownership. Task 2 adds durable JSON artifacts and the size guard. Tasks 3 and 4 add persistent idempotency plus explicit conflict/running/unknown semantics. Task 5 connects them to live execution. Task 6 makes deployment behavior visible. Task 7 verifies SQLite and optional PostgreSQL contracts.
- Placeholder scan: each task names files, tests, commands, expected output, concrete interfaces, state transitions, and commit commands.
- Type consistency: ArtifactStore, build_artifact_store, IdempotencyStore, IdempotencyClaim, build_idempotency_store, artifact_store_factory, and idempotency_store use the same names throughout.

