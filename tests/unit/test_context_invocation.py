from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentkit.core.context.assembler import ContextAssembler
from agentkit.core.context.errors import ContextOutputInvalidError
from agentkit.core.context.invocation import ContextDebugSampler, ContextInvocationService
from agentkit.core.context.registry import ContextRegistry
from tests.context_support import RecordingAudit, render_request, write_context_pack


class FakeClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _assembler(root: Path) -> ContextAssembler:
    return ContextAssembler(ContextRegistry(root=root, tenant_selector="company_alpha"))


def test_invoke_json_validates_schema_and_records_metadata(tmp_path: Path) -> None:
    write_context_pack(tmp_path)
    audit = RecordingAudit()
    service = ContextInvocationService(
        assembler=_assembler(tmp_path),
        audit=audit,
        call_text=lambda system, user: '{"goal":"ok"}',
        model_label="fake-model",
    )

    result = service.invoke_json(render_request())

    assert result.value == {"goal": "ok"}
    event = audit.events_for("r1")[-1]
    assert event["type"] == "llm_context"
    assert event["payload"]["context_id"] == "runtime.intent"
    assert event["payload"]["model"] == "fake-model"
    assert "system" not in event["payload"]
    assert "user" not in event["payload"]


def test_invoke_json_rejects_schema_mismatch_and_audits_failure(tmp_path: Path) -> None:
    write_context_pack(tmp_path)
    audit = RecordingAudit()
    service = ContextInvocationService(
        assembler=_assembler(tmp_path),
        audit=audit,
        call_text=lambda system, user: '{"wrong":true}',
    )

    with pytest.raises(ContextOutputInvalidError):
        service.invoke_json(render_request())

    event = audit.events_for("r1")[-1]
    assert event["type"] == "llm_context_failed"
    assert event["payload"]["error_code"] == "model_output_invalid"
    assert "wrong" not in json.dumps(event, ensure_ascii=False)


def test_invoke_json_supports_array_root_schema_and_markdown_fence(tmp_path: Path) -> None:
    folder = write_context_pack(tmp_path)
    (folder / "output.schema.json").write_text(
        json.dumps({"type": "array", "items": {"type": "string"}}),
        encoding="utf-8",
    )
    service = ContextInvocationService(
        assembler=_assembler(tmp_path),
        call_text=lambda system, user: '```json\n["fact"]\n```',
    )

    result = service.invoke_json(render_request())

    assert result.value == ["fact"]


def test_invoke_streaming_uses_stream_call_for_text_pack(tmp_path: Path) -> None:
    write_context_pack(tmp_path, output_mode="text")
    calls: list[str] = []
    service = ContextInvocationService(
        assembler=_assembler(tmp_path),
        call_text=lambda system, user: "blocking",
        call_stream=lambda system, user: calls.append(user) or "streamed",
    )

    result = service.invoke_streaming(render_request())

    assert result.value == "streamed"
    assert len(calls) == 1


def test_truncation_records_metadata_without_content(tmp_path: Path) -> None:
    write_context_pack(
        tmp_path,
        inputs=[
            {
                "name": "message",
                "source": "request.message",
                "required": True,
                "max_chars": 3,
            }
        ],
    )
    audit = RecordingAudit()
    service = ContextInvocationService(
        assembler=_assembler(tmp_path),
        audit=audit,
        call_text=lambda system, user: '{"goal":"ok"}',
    )

    service.invoke_json(render_request(values={"request.message": "SECRET-LONG"}))

    event = next(item for item in audit.events if item["type"] == "context_truncated")
    assert event["payload"]["details"][0]["before_chars"] == 11
    assert "SECRET" not in json.dumps(event, ensure_ascii=False)


def test_debug_sampler_is_bounded_redacted_and_ephemeral() -> None:
    clock = FakeClock(1000.0)
    sampler = ContextDebugSampler(max_items=2, ttl_seconds=300, clock=clock)

    sampler.add(
        context_id="runtime.intent",
        system="safe",
        user="phone=13800138000 email=a@example.com Authorization: Bearer abc",
    )

    sample = sampler.items()[0]
    assert "13800138000" not in sample["user"]
    assert "a@example.com" not in sample["user"]
    assert "Bearer abc" not in sample["user"]
    clock.value = 1301.0
    assert sampler.items() == []
