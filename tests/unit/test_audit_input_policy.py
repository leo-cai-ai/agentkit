from __future__ import annotations

import hashlib

import pytest

from agentkit.core.audit import InMemoryAuditLog, SQLiteAuditLog, sanitize_audit_input


def test_audit_input_policy_redacts_pii_and_keeps_digest() -> None:
    text = "请联系 user@example.com"

    sanitized = sanitize_audit_input(text, "redacted")

    assert sanitized.text == "请联系 [REDACTED:email]"
    assert sanitized.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert sanitized.length == len(text)


def test_audit_input_policy_hash_mode_does_not_store_plaintext() -> None:
    text = "高度敏感的用户请求"

    sanitized = sanitize_audit_input(text, "hash")

    assert sanitized.text == f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
    assert text not in sanitized.text


@pytest.mark.parametrize(
    "audit_factory",
    [
        lambda tmp_path: InMemoryAuditLog(input_mode="redacted"),
        lambda tmp_path: SQLiteAuditLog(tmp_path / "audit.sqlite", input_mode="redacted"),
    ],
)
def test_audit_logs_apply_input_policy_to_run_and_event(tmp_path, audit_factory) -> None:
    audit = audit_factory(tmp_path)

    run_id = audit.start_run(
        tenant_id="company_alpha",
        user_id="chris",
        text="邮箱 user@example.com",
    )

    run = audit.get_run(run_id)
    event = audit.events_for(run_id)[0]
    assert run is not None
    assert run["text"] == "邮箱 [REDACTED:email]"
    assert event["payload"]["text"] == "邮箱 [REDACTED:email]"
    assert event["payload"]["input_mode"] == "redacted"
    assert event["payload"]["input_length"] == len("邮箱 user@example.com")
    assert len(event["payload"]["input_sha256"]) == 64
