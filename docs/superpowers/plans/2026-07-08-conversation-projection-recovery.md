# Recoverable Conversation Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an append-preserving conversation projection so user input, visible Agent output, approval state, failures, and retries survive refreshes, disconnects, and process restarts.

**Architecture:** Introduce durable Turn, Attempt, Message, and Action projections in the existing SQLite/PostgreSQL conversation stores. The Web layer persists a command before starting the Agent, SSE becomes a transport rather than the source of truth, and Chat renders a server-owned Timeline. General Agent execution, LangGraph Checkpoints, artifacts, audit, memory, and metrics remain separate but linked by stable IDs.

**Tech Stack:** Python 3.11+, Flask, SQLite, PostgreSQL/psycopg, LangGraph, vanilla JavaScript, pytest, Ruff.

## Global Constraints

- User input MUST be durable before General Agent routing or business Agent execution begins.
- Finalized visible messages are append-only; Retry MUST create a new Attempt and MUST NOT call `replace_turn_messages`.
- The only mutable message is an in-progress `streaming` draft; terminal messages are sealed.
- Approval buttons MUST be reconstructed from `conversation_actions`, never solely from browser memory.
- Thinking MUST expose only controlled high-level stages and MUST NOT expose hidden reasoning, prompts, secrets, raw Tool arguments, or stack traces.
- Display Timeline keeps all visible Attempts and revisions; LLM Context loads only the canonical successful output per Turn.
- Every submit, approval, rejection, and retry command MUST have a database-backed idempotency key.
- SQLite and PostgreSQL MUST implement the same behavior and constraints.
- Production MUST reject `approval_checkpointer=memory`; development and test may use it.
- Existing audit, metrics, tenant isolation, RBAC, artifact retention, Tool idempotency, and conversation deletion semantics MUST remain intact.
- UI copy and code comments added by this work MUST use Chinese except stable API/status identifiers.
- No compatibility shim remains after the new Timeline UI is enabled; remove the old retry replacement and browser-only approval recovery paths.

---

## File Structure

### New files

- `src/agentkit/runtime/conversation_projection_models.py`: enums and immutable contracts for Turn, Attempt, Message, Action, Timeline, and accepted commands.
- `src/agentkit/runtime/conversation_projection.py`: business invariants, timeline assembly, context projection, and canonical selection.
- `src/agentkit/runtime/conversation_recovery.py`: reconciliation of queued/running/resuming/waiting Attempts after failures or restarts.
- `src/agentkit/web/static/js/chat_timeline.js`: pure Timeline rendering and controlled Thinking labels.
- `tests/unit/test_conversation_projection_models.py`: state and serialization contract tests.
- `tests/unit/test_conversation_projection_store.py`: SQLite atomic write and idempotency tests.
- `tests/unit/test_conversation_projection.py`: projection service tests.
- `tests/unit/test_conversation_recovery.py`: recovery decision tests.
- `tests/integration/test_conversation_timeline_api.py`: Timeline, command, SSE, approval, and retry API tests.
- `tests/integration/test_conversation_projection_flow.py`: General Agent and XHS lifecycle regression tests.

### Modified files

- `src/agentkit/core/migrations.py`: storage migration version 4.
- `src/agentkit/core/memory/store.py`: SQLite projection persistence.
- `src/agentkit/core/memory/pg_store.py`: PostgreSQL projection persistence.
- `src/agentkit/runtime/bootstrap.py`: construct projection and recovery services; validate checkpointer policy.
- `src/agentkit/runtime/conversation_context.py`: use canonical context messages and exclude the active Turn.
- `src/agentkit/runtime/conversation_persistence.py`: retain memory/summary finalization only; remove retry replacement behavior.
- `src/agentkit/core/multi_agent.py`: consume prepared Attempts, update stages, project outputs and approvals, and finalize failures.
- `src/agentkit/core/langgraph_agent.py`: expose pending approval projection data without making Checkpoint the Chat record.
- `src/agentkit/web/streaming.py`: emit `accepted`, `stage`, and `projection_changed` frames.
- `src/agentkit/web/app.py`: Timeline and command endpoints; remove old message/retry recovery contract.
- `src/agentkit/web/templates/base.html`: load `chat_timeline.js` before `app.js`.
- `src/agentkit/web/templates/chat.html`: Attempt-local controls and Thinking accessibility hooks.
- `src/agentkit/web/static/js/app.js`: server Timeline hydration, disconnect recovery, approval/retry commands, and removal of duplicate fallback POST.
- `src/agentkit/web/static/css/pages.css`: Thinking, Attempt grouping, revision disclosure, approval, and error states.
- `src/agentkit/config.py`: production checkpointer validation helper.
- `tests/unit/test_migrations.py`: SQLite v4 migration and legacy adoption tests.
- `tests/unit/test_postgres_memory_store.py`: PostgreSQL SQL contract tests.
- `tests/unit/test_conversation_context.py`: canonical projection tests.
- `tests/unit/test_multi_agent_service.py`: input-first and approval projection tests.
- `tests/unit/test_config.py`: production checkpointer validation tests.
- `tests/unit/test_streaming.py`: new SSE frame tests.
- `tests/integration/test_chat_api.py`: replace old empty-history and replace-retry expectations.
- `tests/integration/test_web_ui_redesign.py`: Timeline renderer, Thinking, approval recovery, and no-fallback assertions.
- `tests/integration/test_xhs_publish_approval.py`: XHS preview, approval, failure, and retry history regression.
- `docs/ARCHITECTURE.md`: Conversation Projection architecture.
- `docs/framework/06_MEMORY_AND_RAG.md`: canonical Context Projection semantics.
- `docs/framework/07_GOVERNANCE_AND_DURABLE_EXECUTION.md`: durable Action and recovery semantics.
- `docs/framework/REFERENCE.md`: Timeline and command API reference.
- `docs/DEPLOYMENT.md`: durable checkpointer production requirement.

---

### Task 1: Define Projection Contracts and Schema v4

**Files:**
- Create: `src/agentkit/runtime/conversation_projection_models.py`
- Modify: `src/agentkit/core/migrations.py`
- Modify: `src/agentkit/core/memory/store.py`
- Modify: `src/agentkit/core/memory/pg_store.py`
- Test: `tests/unit/test_conversation_projection_models.py`
- Test: `tests/unit/test_migrations.py`

**Interfaces:**
- Produces: `AttemptStatus`, `AttemptStage`, `ActionStatus`, `MessageState`, `AcceptedTurn`, `AttemptRef`, `ApprovalAction`, and `ConversationTimeline`.
- Produces: schema tables `conversation_turns`, `conversation_attempts`, `conversation_actions`, plus projection columns on `messages`.
- Consumes: existing `conversations`, `messages`, `task_runs`, and `workflow_artifacts` tables.

- [ ] **Step 1: Write failing model and migration tests**

```python
def test_projection_statuses_are_stable_string_enums() -> None:
    assert AttemptStatus.WAITING_FOR_APPROVAL.value == "waiting_for_approval"
    assert AttemptStage.ROUTING_AGENT.value == "routing_agent"
    assert ActionStatus.INVALIDATED.value == "invalidated"
    assert MessageState.SEALED.value == "sealed"


def test_sqlite_v4_creates_conversation_projection_schema(tmp_path) -> None:
    db_path = tmp_path / "runtime.sqlite"
    assert run_sqlite_migrations(db_path) == [1, 2, 3, 4]
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"conversation_turns", "conversation_attempts", "conversation_actions"} <= tables
        message_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(messages)")
        }
        assert {
            "turn_id", "attempt_id", "kind", "state", "artifact_id",
            "supersedes_message_id", "visibility", "metadata_json", "updated_at",
        } <= message_columns
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_projection_models.py tests/unit/test_migrations.py::test_sqlite_v4_creates_conversation_projection_schema -q
```

Expected: FAIL because the model module and migration version 4 do not exist.

- [ ] **Step 3: Implement exact projection enums and dataclasses**

```python
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AttemptStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RESUMING = "resuming"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class AttemptStage(StrEnum):
    UNDERSTANDING_REQUEST = "understanding_request"
    ROUTING_AGENT = "routing_agent"
    EXECUTING_AGENT = "executing_agent"
    PREPARING_APPROVAL = "preparing_approval"
    AWAITING_USER_DECISION = "awaiting_user_decision"
    PUBLISHING = "publishing"
    FINALIZING = "finalizing"


class ActionStatus(StrEnum):
    PENDING = "pending"
    DECIDING = "deciding"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"
    INVALIDATED = "invalidated"


class MessageState(StrEnum):
    STREAMING = "streaming"
    SEALED = "sealed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class AcceptedTurn:
    conversation_id: str
    turn_id: str
    attempt_id: str
    user_message_id: int
    created: bool


@dataclass(frozen=True)
class AttemptRef:
    turn_id: str
    attempt_id: str
    attempt_no: int
    status: AttemptStatus
    created: bool


@dataclass(frozen=True)
class ApprovalAction:
    id: str
    attempt_id: str
    status: ActionStatus
    version: int
    thread_id: str
    skills: tuple[str, ...] = ()
    preview: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationTimeline:
    conversation: dict[str, Any]
    turns: tuple[dict[str, Any], ...]
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation": self.conversation,
            "turns": list(self.turns),
            "version": self.version,
        }
```

- [ ] **Step 4: Add migration version 4 for SQLite and PostgreSQL**

Add `_SQLITE_MIGRATIONS[4]` and `_POSTGRES_MIGRATIONS[4]` with the fields in the approved spec. Include these exact constraints:

```sql
CREATE UNIQUE INDEX idx_conversation_turns_client_message
ON conversation_turns(tenant_id, user_id, client_message_id);

CREATE UNIQUE INDEX idx_conversation_attempts_number
ON conversation_attempts(turn_id, attempt_no);

CREATE UNIQUE INDEX idx_conversation_attempts_retry_key
ON conversation_attempts(turn_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX idx_conversation_attempts_one_active
ON conversation_attempts(turn_id)
WHERE status IN ('queued', 'running', 'waiting_for_approval', 'resuming');

CREATE UNIQUE INDEX idx_conversation_actions_idempotency
ON conversation_actions(attempt_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX idx_messages_one_streaming_per_attempt
ON messages(attempt_id)
WHERE attempt_id IS NOT NULL AND state = 'streaming';
```

The PostgreSQL migration uses `JSONB`; SQLite uses `TEXT NOT NULL DEFAULT '{}'`. Update both store `_init_schema` methods so direct store construction creates the same latest schema.

`build_runtime` currently runs `run_storage_migrations` before constructing `ConversationStore`, so migration 4 must be valid against both an empty database and an existing pre-projection database. Refactor the migration helper so it first creates the current base `conversations` and `messages` tables with `CREATE TABLE IF NOT EXISTS`, then conditionally adds only missing projection columns, and finally creates projection tables/indexes. Do not rely on `ConversationStore._init_schema` having run. Add one empty-database test and one existing-v3-database test for SQLite, plus equivalent PostgreSQL SQL-contract assertions. Store `_init_schema` remains an idempotent latest-schema definition and must match the migration result.

- [ ] **Step 5: Run focused migration tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_projection_models.py tests/unit/test_migrations.py tests/unit/test_postgres_memory_store.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```powershell
git add src/agentkit/runtime/conversation_projection_models.py src/agentkit/core/migrations.py src/agentkit/core/memory/store.py src/agentkit/core/memory/pg_store.py tests/unit/test_conversation_projection_models.py tests/unit/test_migrations.py tests/unit/test_postgres_memory_store.py
git commit -m "feat: add conversation projection schema"
```

---

### Task 2: Implement Atomic Turn and Attempt Persistence

**Files:**
- Modify: `src/agentkit/core/memory/store.py`
- Modify: `src/agentkit/core/memory/pg_store.py`
- Create: `tests/unit/test_conversation_projection_store.py`
- Modify: `tests/unit/test_postgres_memory_store.py`
- Modify: `tests/unit/test_conversation_deletion.py`

**Interfaces:**
- Consumes: `AcceptedTurn`, `AttemptRef`, `AttemptStatus`, `AttemptStage` from Task 1.
- Produces: `accept_turn`, `bind_attempt_run`, `transition_attempt`, `get_attempt`, `create_retry_attempt`, and `list_non_terminal_attempts` on both stores.

- [ ] **Step 1: Write failing SQLite behavior tests**

```python
def test_accept_turn_is_idempotent_and_persists_input_before_run(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversation.sqlite")
    first = store.accept_turn(
        tenant_id="tenant-a",
        agent="general_agent",
        user_id="u1",
        conversation_id=None,
        title="研究小红书",
        client_message_id="client-1",
        user_content="研究小红书 Top 5",
        user_token_estimate=8,
    )
    second = store.accept_turn(
        tenant_id="tenant-a",
        agent="general_agent",
        user_id="u1",
        conversation_id=None,
        title="研究小红书",
        client_message_id="client-1",
        user_content="研究小红书 Top 5",
        user_token_estimate=8,
    )
    assert second == first
    assert first.created is True
    assert store.all_messages(first.conversation_id)[0]["content"] == "研究小红书 Top 5"
    assert store.get_attempt(first.attempt_id)["status"] == "queued"


def test_retry_creates_new_attempt_without_copying_user_message(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversation.sqlite")
    accepted = store.accept_turn(
        tenant_id="tenant-a",
        agent="general_agent",
        user_id="u1",
        conversation_id=None,
        title="研究小红书",
        client_message_id="client-1",
        user_content="研究小红书 Top 5",
        user_token_estimate=8,
    )
    store.transition_attempt(
        accepted.attempt_id,
        expected={"queued"},
        status="failed",
        error_code="publish_failed",
        error_summary="发布失败",
    )
    retry = store.create_retry_attempt(
        turn_id=accepted.turn_id,
        retry_of_attempt_id=accepted.attempt_id,
        idempotency_key="retry-1",
    )
    assert retry.attempt_no == 2
    assert store.count_messages(accepted.conversation_id) == 1


def test_delete_conversation_removes_projection_but_keeps_audit(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversation.sqlite")
    accepted = store.accept_turn(
        tenant_id="tenant-a",
        agent="general_agent",
        user_id="u1",
        conversation_id=None,
        title="待删除",
        client_message_id="client-delete",
        user_content="删除这个会话",
        user_token_estimate=6,
    )
    counts = store.delete_conversation(accepted.conversation_id)
    assert counts["turns"] == 1
    assert counts["attempts"] == 1
    assert store.get_conversation(accepted.conversation_id) is None
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_projection_store.py -q
```

Expected: FAIL because the atomic store methods do not exist.

- [ ] **Step 3: Implement SQLite methods in one transaction each**

Use these signatures exactly:

```python
def accept_turn(
    self, *, tenant_id: str, agent: str, user_id: str,
    conversation_id: str | None, title: str, client_message_id: str,
    user_content: str, user_token_estimate: int,
) -> AcceptedTurn: ...

def bind_attempt_run(self, attempt_id: str, *, run_id: str, agent_id: str) -> None: ...

def transition_attempt(
    self, attempt_id: str, *, expected: set[str], status: str,
    stage: str | None = None, error_code: str = "", error_summary: str = "",
) -> bool: ...

def get_attempt(self, attempt_id: str) -> dict[str, Any] | None: ...

def create_retry_attempt(
    self, *, turn_id: str, retry_of_attempt_id: str, idempotency_key: str,
) -> AttemptRef: ...

def list_non_terminal_attempts(self, *, tenant_id: str) -> list[dict[str, Any]]: ...
```

`accept_turn` must query the global `(tenant_id, user_id, client_message_id)` key before creating a Conversation. On duplicate input it returns the original IDs with `created=False`. `create_retry_attempt` must reject non-terminal source Attempts, rely on the active-Attempt unique index to reject races, and return `AttemptRef.created=False` for an idempotent duplicate so the API never starts a second Run.

Update `delete_conversation` to delete Actions, Attempts, Turns, Messages, summaries, and source memories in one transaction while leaving `task_runs` and `audit_events` untouched. Enable `PRAGMA foreign_keys = ON` in every SQLite store connection.

- [ ] **Step 4: Implement PostgreSQL equivalents with row locking**

Use `SELECT ... FOR UPDATE` for Turn retry creation and Action decisions. Translate only placeholders and JSON adaptation; method return values must match SQLite exactly.

- [ ] **Step 5: Run store tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_projection_store.py tests/unit/test_postgres_memory_store.py tests/unit/test_conversation_deletion.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```powershell
git add src/agentkit/core/memory/store.py src/agentkit/core/memory/pg_store.py tests/unit/test_conversation_projection_store.py tests/unit/test_postgres_memory_store.py tests/unit/test_conversation_deletion.py
git commit -m "feat: persist conversation turns and attempts"
```

---

### Task 3: Implement Message Revision and Durable Action Persistence

**Files:**
- Modify: `src/agentkit/core/memory/store.py`
- Modify: `src/agentkit/core/memory/pg_store.py`
- Modify: `tests/unit/test_conversation_projection_store.py`
- Modify: `tests/unit/test_postgres_memory_store.py`

**Interfaces:**
- Consumes: projection schema and enums from Task 1.
- Produces: streaming Message lifecycle and approval Action compare-and-set operations.

- [ ] **Step 1: Write failing message and Action tests**

```python
def accepted_store(tmp_path):
    store = ConversationStore(tmp_path / "conversation.sqlite")
    accepted = store.accept_turn(
        tenant_id="tenant-a",
        agent="general_agent",
        user_id="u1",
        conversation_id=None,
        title="研究小红书",
        client_message_id="client-1",
        user_content="研究小红书 Top 5",
        user_token_estimate=8,
    )
    return store, accepted


def test_review_appends_revision_and_keeps_original(tmp_path) -> None:
    store, accepted = accepted_store(tmp_path)
    original_id = store.open_attempt_message(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        role="assistant",
        kind="assistant_output",
        content="初稿",
        agent_id="xhs_growth",
    )
    store.seal_attempt_message(original_id, content="初稿")
    revision_id = store.append_attempt_revision(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        content="审核后版本",
        agent_id="xhs_growth",
        supersedes_message_id=original_id,
    )
    rows = store.messages_for_attempt(accepted.attempt_id)
    assert [row["content"] for row in rows] == ["初稿", "审核后版本"]
    assert rows[-1]["supersedes_message_id"] == original_id
    assert revision_id != original_id


def test_approval_decision_is_compare_and_set_and_idempotent(tmp_path) -> None:
    store, accepted = accepted_store(tmp_path)
    _, action = store.persist_approval_request(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        agent_id="xhs_growth",
        visible_content="审核后版本",
        thread_id="thread-1",
        skills=["xhs.growth.campaign"],
        preview={"title": "审核后版本"},
        preview_artifact_id=None,
    )
    decided = store.decide_action(
        action.id,
        decision="approved",
        decided_by="u1",
        decision_context={"roles": ["growth_manager"]},
        idempotency_key="approve-1",
        expected_version=action.version,
    )
    repeated = store.decide_action(
        action.id,
        decision="approved",
        decided_by="u1",
        decision_context={"roles": ["growth_manager"]},
        idempotency_key="approve-1",
        expected_version=action.version,
    )
    assert repeated == decided
    assert decided.status is ActionStatus.APPROVED


def test_streaming_message_checkpoints_then_seals_without_duplicate(tmp_path) -> None:
    store, accepted = accepted_store(tmp_path)
    message_id = store.open_attempt_message(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        role="assistant",
        kind="assistant_output",
        content="",
        agent_id="xhs_growth",
    )
    assert store.checkpoint_attempt_message(message_id, content="正在生成") is True
    assert store.seal_attempt_message(message_id, content="最终内容") is True
    assert store.checkpoint_attempt_message(message_id, content="不能再覆盖") is False
    rows = store.messages_for_attempt(accepted.attempt_id)
    assert [(row["id"], row["content"], row["state"]) for row in rows] == [
        (message_id, "最终内容", "sealed")
    ]
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_projection_store.py -q
```

Expected: FAIL on missing Message and Action methods.

- [ ] **Step 3: Implement the exact Message API**

```python
def open_attempt_message(
    self, *, conversation_id: str, turn_id: str, attempt_id: str,
    role: str, kind: str, content: str, agent_id: str,
) -> int: ...

def checkpoint_attempt_message(self, message_id: int, *, content: str) -> bool: ...

def seal_attempt_message(
    self, message_id: int, *, content: str, state: str = "sealed",
) -> bool: ...

def append_attempt_revision(
    self, *, conversation_id: str, turn_id: str, attempt_id: str,
    content: str, agent_id: str, supersedes_message_id: int,
    artifact_id: str | None = None, metadata: dict[str, Any] | None = None,
) -> int: ...

def messages_for_attempt(self, attempt_id: str) -> list[dict[str, Any]]: ...
```

`checkpoint_attempt_message` must update only rows in `streaming` state. `seal_attempt_message` must be a conditional update and return `False` after the message is terminal.

- [ ] **Step 4: Implement the exact Action API**

```python
def persist_approval_request(
    self, *, conversation_id: str, turn_id: str, attempt_id: str,
    agent_id: str, visible_content: str,
    thread_id: str, skills: list[str], preview: dict[str, Any],
    preview_artifact_id: str | None,
) -> tuple[int, ApprovalAction]: ...

def get_action(self, action_id: str) -> dict[str, Any] | None: ...

def decide_action(
    self, action_id: str, *, decision: str, decided_by: str,
    decision_context: dict[str, Any], idempotency_key: str,
    expected_version: int,
) -> ApprovalAction: ...

def transition_action_attempt(
    self, action_id: str, *, expected_action: set[str], action_status: str,
    expected_attempt: set[str], attempt_status: str,
    error_code: str = "", error_summary: str = "",
) -> bool: ...
```

`persist_approval_request` seals/appends the reviewed visible Message, creates the pending Action, and changes the Attempt to `waiting_for_approval` / `awaiting_user_decision` in one transaction. `decide_action` atomically records the decision and changes the Attempt from `waiting_for_approval` to `resuming`; duplicate `idempotency_key` returns the existing result, while a different decision after the first decision raises `ConversationConflictError`. `transition_action_attempt` is the single transaction used for resume success/failure and checkpoint invalidation so Action and Attempt cannot diverge. Use canonical JSON with `ensure_ascii=False` and sorted keys.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_projection_store.py tests/unit/test_postgres_memory_store.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/agentkit/core/memory/store.py src/agentkit/core/memory/pg_store.py tests/unit/test_conversation_projection_store.py tests/unit/test_postgres_memory_store.py
git commit -m "feat: persist conversation revisions and actions"
```

---

### Task 4: Build Conversation Projection and Context Views

**Files:**
- Create: `src/agentkit/runtime/conversation_projection.py`
- Create: `tests/unit/test_conversation_projection.py`
- Modify: `src/agentkit/runtime/conversation_context.py`
- Modify: `src/agentkit/core/metrics.py`
- Modify: `tests/unit/test_conversation_context.py`
- Modify: `tests/unit/test_metrics.py`

**Interfaces:**
- Consumes: store methods from Tasks 2-3 and model contracts from Task 1.
- Produces: `ConversationProjectionService` and `timeline` / `context_messages` read models.

- [ ] **Step 1: Write failing service tests**

```python
def projection_fixture(tmp_path):
    store = ConversationStore(tmp_path / "conversation.sqlite")
    service = ConversationProjectionService(store=store)
    accepted = service.accept_user_message(
        tenant_id="tenant-a",
        user_id="u1",
        conversation_id=None,
        client_message_id="client-1",
        content="用户问题",
        title="用户问题",
    )
    service.bind_run(accepted.attempt_id, run_id="run-1", agent_id="general_agent")
    return service, accepted


def test_timeline_keeps_failed_attempt_and_expands_latest_retry(tmp_path) -> None:
    service, accepted = projection_fixture(tmp_path)
    service.project_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
        content="失败结果",
        status=AttemptStatus.FAILED,
    )
    retry = service.retry_attempt(
        turn_id=accepted.turn_id,
        retry_of_attempt_id=accepted.attempt_id,
        idempotency_key="retry-1",
    )
    timeline = service.timeline(
        conversation_id=accepted.conversation_id,
        tenant_id="tenant-a",
        user_id="u1",
    )
    attempts = timeline.turns[0]["attempts"]
    assert [item["id"] for item in attempts] == [accepted.attempt_id, retry.attempt_id]
    assert attempts[0]["collapsed"] is True
    assert attempts[1]["collapsed"] is False


def test_context_projection_uses_only_canonical_attempt(tmp_path) -> None:
    service, accepted = projection_fixture(tmp_path)
    service.project_output(
        accepted=accepted,
        run_id="run-1",
        agent_id="xhs_growth",
        content="失败结果",
        status=AttemptStatus.FAILED,
    )
    retry = service.retry_attempt(
        turn_id=accepted.turn_id,
        retry_of_attempt_id=accepted.attempt_id,
        idempotency_key="retry-1",
    )
    service.bind_run(retry.attempt_id, run_id="run-2", agent_id="xhs_growth")
    service.project_output(
        accepted=AcceptedTurn(
            conversation_id=accepted.conversation_id,
            turn_id=accepted.turn_id,
            attempt_id=retry.attempt_id,
            user_message_id=accepted.user_message_id,
            created=True,
        ),
        run_id="run-2",
        agent_id="xhs_growth",
        content="成功结果",
        status=AttemptStatus.SUCCEEDED,
    )
    messages = service.context_messages(
        conversation_id=accepted.conversation_id,
        exclude_turn_id=None,
        limit=10,
    )
    assert [item["content"] for item in messages] == ["用户问题", "成功结果"]
    assert "失败结果" not in {item["content"] for item in messages}
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_projection.py tests/unit/test_conversation_context.py -q
```

Expected: FAIL because `ConversationProjectionService` and canonical reads do not exist.

- [ ] **Step 3: Implement the projection service boundary**

```python
class ConversationProjectionService:
    def __init__(
        self, *, store, tokenizer=None, audit=None, metrics=None, clock=time.time,
    ): ...

    def accept_user_message(
        self, *, tenant_id: str, user_id: str, conversation_id: str | None,
        client_message_id: str, content: str, title: str,
    ) -> AcceptedTurn: ...

    def bind_run(self, attempt_id: str, *, run_id: str, agent_id: str) -> None: ...

    def set_stage(self, attempt_id: str, stage: AttemptStage) -> None: ...

    def open_streaming_output(
        self, *, accepted: AcceptedTurn, run_id: str, agent_id: str,
    ) -> int: ...

    def checkpoint_streaming_output(self, message_id: int, *, content: str) -> bool: ...

    def seal_streaming_output(
        self, message_id: int, *, content: str, status: AttemptStatus,
        artifact_id: str | None = None,
    ) -> bool: ...

    def project_output(
        self, *, accepted: AcceptedTurn, run_id: str, agent_id: str,
        content: str, status: AttemptStatus, artifact_id: str | None = None,
    ) -> int: ...

    def request_approval(
        self, *, accepted: AcceptedTurn, run_id: str, agent_id: str,
        thread_id: str, skills: list[str], preview: dict[str, Any],
        preview_artifact_id: str | None = None,
    ) -> ApprovalAction: ...

    def fail_attempt(
        self, attempt_id: str, *, error_code: str, error_summary: str,
    ) -> None: ...

    def retry_attempt(
        self, *, turn_id: str, retry_of_attempt_id: str, idempotency_key: str,
    ) -> AttemptRef: ...

    def timeline(
        self, *, conversation_id: str, tenant_id: str, user_id: str,
    ) -> ConversationTimeline: ...

    def timeline_for_client_message(
        self, *, tenant_id: str, user_id: str, client_message_id: str,
    ) -> ConversationTimeline: ...

    def context_messages(
        self, *, conversation_id: str, exclude_turn_id: str | None, limit: int,
    ) -> list[dict[str, str]]: ...
```

`open_streaming_output` is idempotent per active Attempt and returns the existing streaming Message when called twice. `project_output` atomically seals that existing Message when present and appends a sealed Message only when no streaming Message exists, so the coordinator and transport observer cannot create duplicates. The streaming path checkpoints accumulated content at most once per second or whenever 512 new characters have arrived. A failed/interrupted stream retains its latest checkpoint with terminal state. Successful output marks the Attempt succeeded and sets `canonical_attempt_id`; failed output is retained but never canonical.

Every successful state mutation emits a structured audit event with IDs and status only: `conversation_turn_created`, `conversation_attempt_created`, `conversation_attempt_stage_changed`, `conversation_message_sealed`, `conversation_action_created`, `conversation_action_decided`, `conversation_action_invalidated`, `conversation_attempt_retried`, and `conversation_projection_reconciled`. Event payloads must not include message bodies. Record tenant/Agent-scoped metrics for submit-to-durable latency, stage duration, approval wait, idempotent duplicates, interrupted Attempts, Timeline latency/body size, SSE disconnects, and recovery outcomes. Extend `tests/unit/test_metrics.py` to assert dimensions contain tenant/Agent but no content or raw Tool arguments.

- [ ] **Step 4: Switch ConversationContextService to canonical reads**

Change `ConversationReader` to require:

```python
def context_messages(
    self, *, conversation_id: str, exclude_turn_id: str | None, limit: int,
) -> list[dict[str, Any]]: ...
```

Add `exclude_turn_id` to `build` and `build_for_delegation`. The coordinator will pass the active Turn so the current user input is not duplicated in Prompt context.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_projection.py tests/unit/test_conversation_context.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```powershell
git add src/agentkit/runtime/conversation_projection.py src/agentkit/runtime/conversation_context.py src/agentkit/core/metrics.py tests/unit/test_conversation_projection.py tests/unit/test_conversation_context.py tests/unit/test_metrics.py
git commit -m "feat: add conversation timeline projections"
```

---

### Task 5: Integrate Input-First Persistence with Multi-Agent Execution

**Files:**
- Modify: `src/agentkit/core/multi_agent.py`
- Modify: `src/agentkit/runtime/conversation_persistence.py`
- Modify: `src/agentkit/runtime/bootstrap.py`
- Modify: `tests/unit/test_multi_agent_service.py`
- Create: `tests/integration/test_conversation_projection_flow.py`

**Interfaces:**
- Consumes: `ConversationProjectionService` from Task 4.
- Produces: prepared Turn/Attempt execution, controlled stage updates, terminal projection, and persisted approval request.

- [ ] **Step 1: Replace the old failure expectation with failing input-first tests**

```python
def test_context_failure_preserves_user_input_and_failed_attempt() -> None:
    service, _, audit, _, contexts, projection = _projection_service()
    contexts.build = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("context down"))
    with pytest.raises(RuntimeError, match="context down"):
        service.handle(_request(message="你好", client_message_id="client-1"))
    timeline = projection.timeline_for_client_message(
        tenant_id="tenant-a",
        user_id="u1",
        client_message_id="client-1",
    )
    assert timeline.turns[0]["user_message"]["content"] == "你好"
    assert timeline.turns[0]["attempts"][0]["status"] == "failed"


def test_waiting_approval_is_durable_before_response() -> None:
    service, gateway, _, _, _, projection = _projection_service()
    gateway.next_response = waiting_approval_response()
    response = service.handle(_request(message="发布小红书", client_message_id="client-2"))
    timeline = projection.timeline(
        conversation_id=response.conversation_id,
        tenant_id="tenant-a",
        user_id="u1",
    )
    assert timeline.turns[0]["attempts"][0]["actions"][0]["status"] == "pending"
    assert timeline.turns[0]["attempts"][0]["actions"][0]["preview"]["title"]
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_multi_agent_service.py tests/integration/test_conversation_projection_flow.py -q
```

Expected: FAIL because coordinator persistence still occurs only at terminal completion.

- [ ] **Step 3: Replace coordinator startup with prepared projection IDs**

`MultiAgentCoordinator.handle` must require trusted context keys inserted by the Web command preparation:

```python
turn_id = str(request.context["conversation_turn_id"])
attempt_id = str(request.context["conversation_attempt_id"])
conversation_id = str(request.context["conversation_id"])
parent_run_id = self._audit.start_run(...)
self._projection.bind_run(attempt_id, run_id=parent_run_id, agent_id=GENERAL_AGENT_ID)
self._projection.set_stage(attempt_id, AttemptStage.UNDERSTANDING_REQUEST)
```

Pass `exclude_turn_id=turn_id` into `ConversationContextService`. Set `ROUTING_AGENT` before `_route`, `EXECUTING_AGENT` before delegation, `PREPARING_APPROVAL` before Action creation, and `FINALIZING` before terminal projection.

- [ ] **Step 4: Project every terminal and approval branch**

- `_finish_general` calls `project_output` for completed, clarification, blocked, and failed user-visible results.
- `_delegate` calls `request_approval` when child status is waiting.
- `_resume_started` updates the existing Action and Attempt; it never creates a second user Message.
- `_fail_parent_run` calls `fail_attempt` after recording audit failure.
- Approval preview and formatted visible response are stored before returning waiting status.

- [ ] **Step 5: Reduce ConversationPersistenceService to canonical memory finalization**

Replace `record_turn` with:

```python
def finalize_canonical_turn(
    self, *, tenant_id: str, agent_id: str, user_id: str,
    conversation_id: str, turn_id: str, run_id: str, window_turns: int,
) -> None: ...
```

It reads canonical context messages from the projection, updates summary, and extracts long-term memory only after success. Delete the call to `replace_turn_messages`.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_multi_agent_service.py tests/unit/test_conversation_persistence.py tests/integration/test_conversation_projection_flow.py -q
```

Expected: PASS with old “no persistence on failure” expectations removed.

- [ ] **Step 7: Commit Task 5**

```powershell
git add src/agentkit/core/multi_agent.py src/agentkit/runtime/conversation_persistence.py src/agentkit/runtime/bootstrap.py tests/unit/test_multi_agent_service.py tests/unit/test_conversation_persistence.py tests/integration/test_conversation_projection_flow.py
git commit -m "feat: persist chat input before agent execution"
```

---

### Task 6: Add Durable Approval Decisions and Recovery Coordination

**Files:**
- Create: `src/agentkit/runtime/conversation_recovery.py`
- Modify: `src/agentkit/core/multi_agent.py`
- Modify: `src/agentkit/core/langgraph_agent.py`
- Modify: `src/agentkit/runtime/bootstrap.py`
- Create: `tests/unit/test_conversation_recovery.py`
- Modify: `tests/integration/test_approval_resume.py`
- Modify: `tests/integration/test_xhs_publish_approval.py`

**Interfaces:**
- Consumes: durable Actions and Attempts from Tasks 3-5.
- Produces: `decide_action`, `resume_action`, and `ConversationRecoveryService.reconcile`.

- [ ] **Step 1: Write failing recovery tests**

```python
def accepted_store(tmp_path):
    store = ConversationStore(tmp_path / "conversation.sqlite")
    accepted = store.accept_turn(
        tenant_id="tenant-a",
        agent="general_agent",
        user_id="u1",
        conversation_id=None,
        title="研究小红书",
        client_message_id="client-1",
        user_content="研究小红书 Top 5",
        user_token_estimate=8,
    )
    return store, accepted


class FakeRecoveryCoordinator:
    def __init__(self, store, *, checkpoint_exists: bool) -> None:
        self.store = store
        self.checkpoint_exists = checkpoint_exists
        self.resumed_threads: list[str] = []

    def pending_approval(self, thread_id: str) -> bool:
        return self.checkpoint_exists

    def resume_action(self, action_id: str) -> None:
        action = self.store.get_action(action_id)
        self.resumed_threads.append(action["thread_id"])
        self.store.transition_action_attempt(
            action_id,
            expected_action={"approved"},
            action_status="completed",
            expected_attempt={"resuming"},
            attempt_status="succeeded",
        )


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def record(self, run_id: str, event_type: str, payload: dict) -> None:
        self.events.append((run_id, event_type, payload))


def recovery_fixture(tmp_path, *, checkpoint_exists: bool, approved: bool):
    store, accepted = accepted_store(tmp_path)
    store.bind_attempt_run(
        accepted.attempt_id,
        run_id="run-1",
        agent_id="xhs_growth",
    )
    store.transition_attempt(
        accepted.attempt_id,
        expected={"queued"},
        status="running",
    )
    _, action = store.persist_approval_request(
        conversation_id=accepted.conversation_id,
        turn_id=accepted.turn_id,
        attempt_id=accepted.attempt_id,
        agent_id="xhs_growth",
        visible_content="审核后版本",
        thread_id="thread-1",
        skills=["xhs.growth.campaign"],
        preview={"title": "审核后版本"},
        preview_artifact_id=None,
    )
    if approved:
        action = store.decide_action(
            action.id,
            decision="approved",
            decided_by="u1",
            decision_context={"roles": ["growth_manager"]},
            idempotency_key="approve-1",
            expected_version=action.version,
        )
    coordinator = FakeRecoveryCoordinator(
        store,
        checkpoint_exists=checkpoint_exists,
    )
    recovery = ConversationRecoveryService(
        store=store,
        coordinator=coordinator,
        audit=RecordingAudit(),
    )
    return store, coordinator, recovery, action


def test_approved_action_with_pending_checkpoint_resumes_once(tmp_path) -> None:
    store, gateway, recovery, action = recovery_fixture(
        tmp_path,
        checkpoint_exists=True,
        approved=True,
    )
    recovery.reconcile(tenant_id="tenant-a")
    recovery.reconcile(tenant_id="tenant-a")
    assert gateway.resumed_threads == [action.thread_id]
    assert store.get_action(action.id)["status"] == "completed"


def test_missing_checkpoint_invalidates_action_but_keeps_messages(tmp_path) -> None:
    store, _, recovery, action = recovery_fixture(
        tmp_path,
        checkpoint_exists=False,
        approved=False,
    )
    before = store.messages_for_attempt(action.attempt_id)
    recovery.reconcile(tenant_id="tenant-a")
    assert store.get_action(action.id)["status"] == "invalidated"
    assert store.get_attempt(action.attempt_id)["status"] == "interrupted"
    assert store.messages_for_attempt(action.attempt_id) == before
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_recovery.py tests/integration/test_approval_resume.py -q
```

Expected: FAIL because recovery coordination does not exist.

- [ ] **Step 3: Implement Action-based resume**

Add to `MultiAgentCoordinator`:

```python
def decide_action(
    self, action_id: str, *, decision: str, decided_by: str,
    decision_context: dict[str, Any], idempotency_key: str,
    expected_version: int,
) -> TaskResponse: ...

def resume_action(self, action_id: str) -> TaskResponse: ...
```

The browser no longer supplies trusted `thread_id` or Skills. The coordinator loads them from Action, validates the parent/child run relationship, and calls `gateway.resume` with the stored decision.

- [ ] **Step 4: Implement ConversationRecoveryService**

```python
class ConversationRecoveryService:
    def __init__(self, *, store, coordinator, audit, clock=time.time): ...

    def reconcile(self, *, tenant_id: str) -> list[str]: ...
```

Rules:

- stale queued without `run_id` becomes interrupted;
- running with terminal audit Run is projected to that terminal status;
- resuming with an approved/rejected Action and live Checkpoint calls `resume_action` once;
- waiting/resuming without Checkpoint invalidates Action and interrupts Attempt;
- every transition uses version/expected-state compare-and-set.

- [ ] **Step 5: Add a read-only pending-state check to LangGraph runtime**

Expose:

```python
def pending_approval(self, thread_id: str) -> bool:
    snapshot = self._graph.get_state({"configurable": {"thread_id": thread_id}})
    return bool(snapshot.values and snapshot.next)
```

No Chat content is reconstructed from the Checkpoint; it is used only to decide whether resume remains possible.

- [ ] **Step 6: Wire recovery after runtime construction and verify GREEN**

Build recovery after coordinator construction. Call `reconcile` once at startup after all dependencies exist, and expose it on `AgentKitRuntime` for tests and operational repair.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_recovery.py tests/integration/test_approval_resume.py tests/integration/test_xhs_publish_approval.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 6**

```powershell
git add src/agentkit/runtime/conversation_recovery.py src/agentkit/core/multi_agent.py src/agentkit/core/langgraph_agent.py src/agentkit/runtime/bootstrap.py tests/unit/test_conversation_recovery.py tests/integration/test_approval_resume.py tests/integration/test_xhs_publish_approval.py
git commit -m "feat: recover durable conversation approvals"
```

---

### Task 7: Replace Chat Recovery APIs with Timeline Commands

**Files:**
- Modify: `src/agentkit/web/streaming.py`
- Modify: `src/agentkit/web/app.py`
- Modify: `tests/unit/test_streaming.py`
- Create: `tests/integration/test_conversation_timeline_api.py`
- Modify: `tests/integration/test_chat_api.py`

**Interfaces:**
- Consumes: projection and Action services from Tasks 4-6.
- Produces: Timeline GET, Action decision POST, Retry POST, and accepted-first SSE.

- [ ] **Step 1: Write failing API tests**

```python
def sse_frames(response) -> list[dict]:
    frames: list[dict] = []
    event = "message"
    for line in response.get_data(as_text=True).splitlines():
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            frames.append(
                {"event": event, "data": json.loads(line.removeprefix("data: "))}
            )
    return frames


def test_stream_accepts_and_persists_turn_before_agent_failure(client) -> None:
    token = _login(client)
    response = client.post(
        "/api/chat/stream",
        json={"message": "你好", "client_message_id": "client-1"},
        headers={"X-CSRF-Token": token},
    )
    frames = sse_frames(response)
    accepted = next(frame for frame in frames if frame["event"] == "accepted")
    timeline = client.get(
        f"/api/conversations/{accepted['data']['conversation_id']}/timeline"
    ).get_json()
    assert timeline["turns"][0]["user_message"]["content"] == "你好"


def test_retry_endpoint_appends_attempt_and_keeps_first_attempt(client) -> None:
    from agentkit.web.app import get_runtime

    token = _login(client)
    runtime = get_runtime()
    accepted = runtime.conversation_projection.accept_user_message(
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        user_id="console-admin",
        conversation_id=None,
        client_message_id="failed-client-1",
        content="研究小红书 Top 5",
        title="研究小红书 Top 5",
    )
    runtime.conversation_projection.bind_run(
        accepted.attempt_id,
        run_id="failed-run-1",
        agent_id="xhs_growth",
    )
    runtime.conversation_projection.fail_attempt(
        accepted.attempt_id,
        error_code="publish_failed",
        error_summary="发布失败",
    )
    response = client.post(
        f"/api/conversation-turns/{accepted.turn_id}/attempts",
        json={
            "retry_of_attempt_id": accepted.attempt_id,
            "idempotency_key": "retry-1",
        },
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    frames = sse_frames(response)
    assert next(frame for frame in frames if frame["event"] == "accepted")["data"][
        "attempt_id"
    ]
    timeline = client.get(
        f"/api/conversations/{accepted.conversation_id}/timeline"
    ).get_json()
    assert len(timeline["turns"][0]["attempts"]) == 2


def test_timeline_rejects_foreign_tenant_and_user_scope(client) -> None:
    from agentkit.web.app import get_runtime

    _login(client)
    runtime = get_runtime()
    tenant_id = str(runtime.tenant_config["tenant_id"])
    foreign_user_id = runtime.conversations.create_conversation(
        tenant_id=tenant_id,
        agent="general_agent",
        user_id="not-console-admin",
        title="其他用户",
    )
    foreign_tenant_id = runtime.conversations.create_conversation(
        tenant_id="another-tenant",
        agent="general_agent",
        user_id="console-admin",
        title="其他租户",
    )

    assert client.get(
        f"/api/conversations/{foreign_user_id}/timeline"
    ).status_code == 404
    assert client.get(
        f"/api/conversations/{foreign_tenant_id}/timeline"
    ).status_code == 404
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_streaming.py tests/integration/test_conversation_timeline_api.py tests/integration/test_chat_api.py -q
```

Expected: FAIL because new endpoints and SSE events do not exist.

- [ ] **Step 3: Extend stream_response with typed initial events**

```python
def stream_response(
    produce: Callable[[], dict[str, Any]], *,
    initial_events: tuple[tuple[str, dict[str, Any]], ...] = (),
    token_observer: Callable[[str], None] | None = None,
    continue_on_disconnect: bool = False,
    max_queue_size: int = _DEFAULT_QUEUE_SIZE,
    stream_tokens: bool = True,
    error_context: dict[str, Any] | None = None,
) -> Iterator[str]: ...
```

Emit `initial_events` after `: stream-open` and before worker token/final frames. The worker calls `token_observer` with each emitted chunk before enqueueing the SSE token. The Chat route supplies a thread-safe accumulator that calls `checkpoint_streaming_output` no more often than once per second unless at least 512 new characters accumulated. On final/error it flushes and seals the same Message. Observer failure is audited and logged but must not suppress the client stream or create a second Message.

For Chat submit and Retry, pass `continue_on_disconnect=True`. The token observer lazily calls `open_streaming_output` on the first token and holds only the returned Message ID and accumulated text. When the SSE generator closes, stop enqueueing client frames and discard future transport tokens, but continue invoking the observer and do not raise `StreamCancelled` inside the producer; the already accepted Attempt continues and persists checkpoints/final state. The coordinator's terminal `project_output` seals the same open Message. Keep cancellation available for non-durable diagnostic streams by leaving the default `False`. Add a unit test that closes the iterator after `accepted`, releases a blocked producer, and asserts the producer reaches terminal projection.

- [ ] **Step 4: Prepare the command before starting SSE**

`api_chat_stream` must:

1. validate identity and payload;
2. require or generate `client_message_id`;
3. call `projection.accept_user_message` synchronously;
4. build a trusted TaskRequest containing Turn and Attempt IDs;
5. emit `accepted` with stable IDs;
6. execute coordinator in the worker only when `AcceptedTurn.created=True`; an idempotent duplicate emits the existing projection and does not launch a second Run.

The blocking `/api/chat` path uses the same preparation helper.

- [ ] **Step 5: Add and secure the new endpoints**

```text
GET  /api/conversations/<conversation_id>/timeline
POST /api/conversation-actions/<action_id>/decision
POST /api/conversation-turns/<turn_id>/attempts
```

Timeline requires `CHAT_USE`. Approval decision requires `TASK_APPROVE`. Retry requires `CHAT_USE`. All relations are loaded server-side and checked against tenant/user scope.

Return `404` rather than revealing whether a foreign tenant/user Conversation, Turn, Attempt, or Action exists. Add equivalent foreign-scope tests for Timeline, approval, and Retry; do not trust IDs or Agent names supplied by the browser.

The Retry endpoint returns accepted-first SSE from the new `/api/conversation-turns/<turn_id>/attempts` URL. It atomically creates Attempt N+1, reconstructs the trusted TaskRequest from the Turn's persisted user Message plus the current authenticated principal, and runs the coordinator only if `AttemptRef.created=True`. A duplicate idempotency key returns the existing Attempt projection without re-execution. The frontend shows Thinking immediately after `accepted` and rehydrates Timeline on every projection change or disconnect.

- [ ] **Step 6: Remove old recovery contract**

Delete:

- `POST /api/conversations/<conversation_id>/retry/stream`;
- execution-only recovery in `GET .../messages`;
- browser-supplied approval `thread_id` and Skills in `_approval`;
- tests expecting an empty message list after a failed run;
- tests expecting Retry to replace old messages.

- [ ] **Step 7: Run focused API tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_streaming.py tests/integration/test_conversation_timeline_api.py tests/integration/test_chat_api.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 7**

```powershell
git add src/agentkit/web/streaming.py src/agentkit/web/app.py tests/unit/test_streaming.py tests/integration/test_conversation_timeline_api.py tests/integration/test_chat_api.py
git commit -m "feat: expose recoverable chat timeline api"
```

---

### Task 8: Render Timeline, Thinking, Approval, Failure, and Retry in Chat UI

**Files:**
- Create: `src/agentkit/web/static/js/chat_timeline.js`
- Modify: `src/agentkit/web/templates/base.html`
- Modify: `src/agentkit/web/templates/chat.html`
- Modify: `src/agentkit/web/static/js/app.js`
- Modify: `src/agentkit/web/static/css/pages.css`
- Modify: `tests/integration/test_web_ui_redesign.py`

**Interfaces:**
- Consumes: Timeline and Command APIs from Task 7.
- Produces: `window.AgentKitChatTimeline` with pure render helpers.

- [ ] **Step 1: Write failing static/UI contract tests**

```python
def test_timeline_renderer_loads_before_app(client) -> None:
    html = client.get("/chat").get_data(as_text=True)
    assert html.index("chat_timeline.js") < html.index("app.js")


def test_chat_uses_timeline_and_never_reposts_after_stream_failure(client) -> None:
    js = client.get("/static/js/app.js").get_data(as_text=True)
    assert "/timeline" in js
    assert "client_message_id" in js
    assert "postChat(requestPayload" not in js


def test_thinking_animation_has_reduced_motion_fallback(client) -> None:
    css = client.get("/static/css/pages.css").get_data(as_text=True)
    assert ".ak-thinking-bars" in css
    assert "prefers-reduced-motion: reduce" in css
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py -q
```

Expected: FAIL on missing Timeline renderer and old fallback POST.

- [ ] **Step 3: Implement pure Timeline helpers**

Expose:

```javascript
window.AgentKitChatTimeline = Object.freeze({
  thinkingLabel(stage),
  hasActiveAttempt(timeline),
  latestRetryableAttempt(timeline),
  render(root, timeline, handlers),
});
```

`thinkingLabel` maps only approved stage identifiers to Chinese labels. Unknown stages return `正在处理`.

- [ ] **Step 4: Implement Attempt-local rendering**

Rendering rules:

- show each user Message once at Turn level;
- expand only the latest Attempt by default;
- show old failed Attempts as disclosure rows;
- show only the newest Review revision, with older revisions in a disclosure;
- show Approval buttons only for pending Action;
- show approval decision plus failure/retry controls after approved execution fails;
- never add a conversation-global status card below the thread.

- [ ] **Step 5: Add stage-aware Thinking with accessibility**

Use four animated bars and a textual live region:

```html
<div class="ak-thinking" role="status" aria-live="polite">
  <span class="ak-thinking-bars" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
  <span data-thinking-label>正在处理</span>
</div>
```

Animate only `transform` and `opacity`. Under reduced motion, disable animation and keep the text label.

- [ ] **Step 6: Replace browser-only pending approval and duplicate fallback behavior**

- Remove `pendingApproval` as the source of truth.
- After every accepted/final/error frame, fetch Timeline by stable Conversation ID.
- On network error, show a connection notice and fetch Timeline; do not call `postChat`.
- Approval and Retry buttons send a generated UUID idempotency key and then rehydrate Timeline.
- Keep the Composer fixed at the bottom; disable it only while the current Conversation has an active Attempt.

- [ ] **Step 7: Run UI contract tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_web_ui_redesign.py -q
```

Expected: PASS.

- [ ] **Step 8: Manually verify the four confirmed visual states**

Start the server:

```powershell
.\.venv\Scripts\agentkit.exe --tenant company_alpha web
```

Verify in the browser:

1. running Thinking;
2. refreshed pending approval;
3. approved then failed Attempt;
4. old failed Attempt collapsed with new Retry expanded.

Stop the test server after verification.

- [ ] **Step 9: Commit Task 8**

```powershell
git add src/agentkit/web/static/js/chat_timeline.js src/agentkit/web/templates/base.html src/agentkit/web/templates/chat.html src/agentkit/web/static/js/app.js src/agentkit/web/static/css/pages.css tests/integration/test_web_ui_redesign.py
git commit -m "feat: render recoverable chat attempts"
```

---

### Task 9: Add Legacy Backfill and Production Configuration Guard

**Files:**
- Modify: `src/agentkit/core/migrations.py`
- Modify: `src/agentkit/config.py`
- Modify: `src/agentkit/runtime/bootstrap.py`
- Modify: `tests/unit/test_migrations.py`
- Modify: `tests/unit/test_config.py`
- Modify: `tests/unit/test_dependency_warnings.py`

**Interfaces:**
- Consumes: schema and services from Tasks 1-8.
- Produces: deterministic legacy history adoption and production startup validation.

- [ ] **Step 1: Write failing backfill and config tests**

```python
def legacy_conversation_database(tmp_path):
    db_path = tmp_path / "legacy.sqlite"
    store = ConversationStore(db_path)
    conversation_id = store.create_conversation(
        tenant_id="tenant-a",
        agent="general_agent",
        user_id="u1",
        title="旧会话",
    )
    store.add_message(
        conversation_id=conversation_id,
        role="user",
        content="问题一",
        run_id="run-1",
    )
    store.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content="结果一",
        run_id="run-1",
        agent_id="general_agent",
    )
    store.add_message(
        conversation_id=conversation_id,
        role="user",
        content="问题二",
        run_id=None,
    )
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_runs (
                run_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                agent_id TEXT,
                parent_run_id TEXT,
                conversation_id TEXT
            );
            INSERT INTO schema_migrations(version, applied_at)
            VALUES (1, 1), (2, 1), (3, 1), (4, 1);
            """
        )
    return db_path


def read_rows(db_path, table):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def read_message_contents(db_path):
    return [row["content"] for row in read_rows(db_path, "messages")]


def test_v5_backfills_legacy_messages_without_rewriting_content(tmp_path) -> None:
    db_path = legacy_conversation_database(tmp_path)
    before = read_message_contents(db_path)
    assert run_sqlite_migrations(db_path) == [5]
    assert read_message_contents(db_path) == before
    turns = read_rows(db_path, "conversation_turns")
    attempts = read_rows(db_path, "conversation_attempts")
    assert len(turns) == 2
    assert all(item["source"] == "legacy_imported" for item in attempts)


def test_production_rejects_memory_approval_checkpointer(monkeypatch) -> None:
    monkeypatch.setenv("AGENTKIT_RUNTIME_ENVIRONMENT", "production")
    monkeypatch.setenv("AGENTKIT_APPROVAL_CHECKPOINTER", "memory")
    with pytest.raises(ValueError, match="durable approval checkpointer"):
        validate_runtime_settings(get_settings())
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_migrations.py tests/unit/test_config.py tests/unit/test_dependency_warnings.py -q
```

Expected: FAIL because migration 5 does not exist and production validation permits memory.

- [ ] **Step 3: Implement deterministic legacy adoption**

For each Conversation in Message ID order:

- pair an adjacent user Message with following assistant Message;
- create one Turn per user Message;
- map same non-empty `run_id` to one Attempt;
- create a synthetic Attempt with `source=legacy_imported` for missing `run_id`;
- mark unmatched user-only Turn interrupted;
- for empty Conversation with a root `task_runs.text`, create a user Message and interrupted Attempt;
- never rewrite existing `content`;
- do not fabricate missing historical approval previews.

Implement adoption as a new migration 5. Do not edit migration 4 after it may have been recorded in a database. Implement SQLite migration 5 with an explicit helper and PostgreSQL migration 5 with equivalent set-based queries under the existing advisory migration lock.

- [ ] **Step 4: Add production checkpointer validation**

```python
def validate_runtime_settings(settings: Settings) -> None:
    if (
        settings.runtime_environment == "production"
        and settings.approval_checkpointer in {"memory", "none"}
    ):
        raise ValueError(
            "production requires a durable approval checkpointer: sqlite or postgres"
        )
```

Change the safe default of `Settings.approval_checkpointer` from `memory` to `sqlite`. Call `validate_runtime_settings` at the start of `build_runtime` before migrations or external connections. Explicit development/test configuration may still choose `memory`.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_migrations.py tests/unit/test_config.py tests/unit/test_dependency_warnings.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 9**

```powershell
git add src/agentkit/core/migrations.py src/agentkit/config.py src/agentkit/runtime/bootstrap.py tests/unit/test_migrations.py tests/unit/test_config.py tests/unit/test_dependency_warnings.py
git commit -m "feat: adopt legacy chat history safely"
```

---

### Task 10: Prove XHS Approval Failure Recovery and Remove Legacy Paths

**Files:**
- Modify: `tests/integration/test_xhs_publish_approval.py`
- Modify: `tests/integration/test_chat_api.py`
- Modify: `tests/integration/test_conversation_projection_flow.py`
- Modify: `src/agentkit/core/memory/store.py`
- Modify: `src/agentkit/core/memory/pg_store.py`
- Modify: `src/agentkit/runtime/conversation_persistence.py`
- Modify: `src/agentkit/web/app.py`
- Modify: `src/agentkit/web/static/js/app.js`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/framework/06_MEMORY_AND_RAG.md`
- Modify: `docs/framework/07_GOVERNANCE_AND_DURABLE_EXECUTION.md`
- Modify: `docs/framework/REFERENCE.md`
- Modify: `docs/DEPLOYMENT.md`

**Interfaces:**
- Consumes: the complete Conversation Projection implementation.
- Produces: end-to-end regression coverage, clean removal of obsolete APIs, and current documentation.

- [ ] **Step 1: Write the exact failing XHS regression**

```python
def test_xhs_approval_failure_refresh_and_retry_preserve_every_visible_record(
    monkeypatch, tmp_path,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        llm_client,
        "_get_provider",
        lambda: FakeProvider(responder=_responder(calls)),
    )
    monkeypatch.setenv("AGENTKIT_RUNTIME_ENVIRONMENT", "test")
    monkeypatch.setenv("AGENTKIT_APPROVAL_CHECKPOINTER", "sqlite")
    monkeypatch.setenv("AGENTKIT_XHS_RESEARCH_PROVIDER", "mock")
    monkeypatch.setenv("AGENTKIT_XHS_PUBLISHING_PROVIDER", "mock")
    config_mod.get_settings.cache_clear()
    runtime = build_runtime(db_path=tmp_path / "audit.sqlite")
    request = _request()
    accepted = runtime.conversation_projection.accept_user_message(
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        user_id=request.user_id,
        conversation_id=None,
        client_message_id="xhs-turn-1",
        content=request.text,
        title=request.text[:60],
    )
    prepared = replace(
        request,
        context={
            **request.context,
            "conversation_id": accepted.conversation_id,
            "conversation_turn_id": accepted.turn_id,
            "conversation_attempt_id": accepted.attempt_id,
        },
    )
    waiting_response = runtime.chat_service.handle(prepared)
    waiting = runtime.conversation_projection.timeline(
        conversation_id=accepted.conversation_id,
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        user_id=request.user_id,
    ).to_dict()
    turn = waiting["turns"][0]
    attempt_1 = turn["attempts"][0]
    action = attempt_1["actions"][0]
    assert action["status"] == "pending"
    assert action["preview"]["title"]

    monkeypatch.setattr(
        runtime.gateway,
        "resume",
        lambda *args, **kwargs: TaskResponse(
            status="failed",
            output={"message": "发布未完成", "error_code": "publish_failed"},
            run_id=waiting_response.governance["delegation"]["child_run_id"],
            thread_id=waiting_response.thread_id,
            agent="xhs_growth",
            strategy="workflow",
            conversation_id=accepted.conversation_id,
            governance={},
            audit_events=[],
        ),
    )
    runtime.chat_service.decide_action(
        action["id"],
        decision="approved",
        decided_by=request.user_id,
        decision_context={"roles": request.roles},
        idempotency_key="approve-xhs-1",
        expected_version=action["version"],
    )
    refreshed = runtime.conversation_projection.timeline(
        conversation_id=accepted.conversation_id,
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        user_id=request.user_id,
    ).to_dict()
    failed = refreshed["turns"][0]["attempts"][0]
    assert refreshed["turns"][0]["user_message"]["content"] == request.text
    assert failed["actions"][0]["status"] == "approved"
    assert failed["status"] == "failed"
    assert failed["messages"]

    runtime.conversation_projection.retry_attempt(
        turn_id=turn["id"],
        retry_of_attempt_id=failed["id"],
        idempotency_key="retry-xhs-1",
    )
    rerun = runtime.conversation_projection.timeline(
        conversation_id=accepted.conversation_id,
        tenant_id=str(runtime.tenant_config["tenant_id"]),
        user_id=request.user_id,
    ).to_dict()
    assert len(rerun["turns"][0]["attempts"]) == 2
    assert rerun["turns"][0]["attempts"][0]["collapsed"] is True
    assert rerun["turns"][0]["attempts"][1]["collapsed"] is False
```

- [ ] **Step 2: Run and verify RED if any legacy path remains**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_xhs_publish_approval.py tests/integration/test_conversation_projection_flow.py tests/integration/test_chat_api.py -q
```

Expected before cleanup: FAIL if any old replace/recovery path still mutates or hides history.

- [ ] **Step 3: Delete obsolete production paths**

Remove all uses and definitions of:

- `replace_turn_messages`;
- `retry_of_run_id` as a message replacement instruction;
- `/api/conversations/<conversation_id>/retry/stream`;
- Chat recovery based on empty `messages` plus `ConversationExecution`;
- browser-owned `pendingApproval` as authoritative state;
- automatic blocking POST fallback after failed SSE.

Keep audit parent/child Run relationships and `ConversationRunStateResolver` only for operational tracing/deletion decisions where still needed.

- [ ] **Step 4: Update active documentation**

Document:

- Turn / Attempt / Message / Action architecture;
- input-first persistence;
- durable approval requirement;
- Timeline and command endpoints;
- canonical Context Projection;
- production `approval_checkpointer=sqlite|postgres` requirement;
- behavior when a Checkpoint is invalidated.

- [ ] **Step 5: Run the targeted regression suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_conversation_projection_models.py tests/unit/test_conversation_projection_store.py tests/unit/test_conversation_projection.py tests/unit/test_conversation_recovery.py tests/unit/test_conversation_context.py tests/unit/test_multi_agent_service.py tests/unit/test_streaming.py tests/integration/test_conversation_timeline_api.py tests/integration/test_conversation_projection_flow.py tests/integration/test_xhs_publish_approval.py tests/integration/test_chat_api.py tests/integration/test_web_ui_redesign.py -q
```

Expected: PASS.

- [ ] **Step 6: Run full verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\agentkit.exe --tenant company_alpha validate-catalog
```

Expected:

- all tests pass;
- Ruff reports no errors;
- format check reports all files formatted;
- catalog validation reports the enabled Agent/Skill/Tool catalog is valid.

- [ ] **Step 7: Confirm no test services remain running**

Use the repository's normal process inspection command and stop only services started during this implementation. Do not stop user-owned processes.

- [ ] **Step 8: Commit Task 10**

```powershell
git add src tests docs
git commit -m "fix: preserve recoverable chat history"
```

---

## Final Acceptance Checklist

- [ ] A user Message exists before routing begins.
- [ ] A failed route, Agent call, approval resume, or Tool execution cannot erase the user Message.
- [ ] Waiting approval survives refresh with preview and buttons.
- [ ] Approved-then-failed execution keeps preview, decision, failure summary, and Retry.
- [ ] Retry creates Attempt N+1 and leaves previous Attempts unchanged.
- [ ] SSE disconnect rehydrates Timeline and never creates a duplicate Turn.
- [ ] Thinking shows only controlled stages and honors reduced motion.
- [ ] Display Timeline retains visible history while LLM Context uses only canonical outputs.
- [ ] SQLite/PostgreSQL behavior, tenant isolation, RBAC, audit, metrics, and Tool idempotency remain covered.
- [ ] Production rejects non-durable approval checkpointers.
- [ ] Full tests, Ruff, formatting, and catalog validation pass.
- [ ] Test services started by implementation are stopped.
