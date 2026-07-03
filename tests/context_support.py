from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from agentkit.core.context.models import ContextRenderRequest


def write_context_pack(
    root: Path,
    *,
    context_id: str = "runtime.intent",
    inputs: list[dict[str, Any]] | None = None,
    output_mode: str = "json",
    instructions: dict[str, bool] | None = None,
    max_input_tokens: int = 2000,
    response_reserve_tokens: int = 300,
) -> Path:
    for name in ("security-boundary", "untrusted-data", "no-hidden-reasoning"):
        fragment = root / "fragments" / f"{name}.md"
        fragment.parent.mkdir(parents=True, exist_ok=True)
        fragment.write_text(name, encoding="utf-8")

    parts = context_id.split(".")
    base = "runtime" if parts[0] == "runtime" else "skills"
    folder = root / base / Path(*parts[1:])
    folder.mkdir(parents=True, exist_ok=True)
    declared = inputs or [
        {
            "name": "message",
            "source": "request.message",
            "required": True,
            "priority": 100,
            "max_chars": 1000,
        }
    ]
    definition: dict[str, Any] = {
        "id": context_id,
        "version": 1,
        "owner": parts[0],
        "templates": {"system": "system.md", "user": "user.md"},
        "instructions": instructions or {"agent": False, "skill": False},
        "inputs": declared,
        "limits": {
            "max_input_tokens": max_input_tokens,
            "response_reserve_tokens": response_reserve_tokens,
        },
        "output": {"mode": output_mode},
    }
    if output_mode == "json":
        definition["output"]["schema"] = "output.schema.json"
        (folder / "output.schema.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "required": ["goal"],
                    "properties": {"goal": {"type": "string"}},
                    "additionalProperties": False,
                }
            ),
            encoding="utf-8",
        )
    (folder / "context.yaml").write_text(
        yaml.safe_dump(definition, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (folder / "system.md").write_text("SYSTEM", encoding="utf-8")
    (folder / "user.md").write_text(
        "\n".join(f"{{{{ {item['name']} }}}}" for item in declared),
        encoding="utf-8",
    )
    return folder


def fake_agent() -> Any:
    return SimpleNamespace(
        name="customer_service",
        instructions="客服边界",
        max_tokens=10_000,
    )


def fake_skill() -> Any:
    return SimpleNamespace(
        name="order.lookup",
        skill_instructions="只读订单查询",
    )


def render_request(
    *,
    context_id: str = "runtime.intent",
    values: dict[str, Any] | None = None,
) -> ContextRenderRequest:
    return ContextRenderRequest(
        context_id=context_id,
        tenant_id="AI-ABC",
        tenant_selector="company_alpha",
        run_id="r1",
        agent=fake_agent(),
        skill=fake_skill(),
        values=values or {"request.message": "hello"},
        global_token_limit=4000,
    )


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append({"run_id": run_id, "type": event_type, "payload": payload})

    def events_for(self, run_id: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["run_id"] == run_id]


class SpyContextInvoker:
    def __init__(self, *values: Any) -> None:
        self.values = list(values)
        self.requests: list[ContextRenderRequest] = []
        self.manifest_hash = "sha256:test"

    def _result(self, request: ContextRenderRequest) -> Any:
        self.requests.append(request)
        value = self.values.pop(0)
        return SimpleNamespace(value=value, estimated_output_tokens=1, rendered=None)

    invoke_json = _result
    invoke_text = _result
    invoke_streaming = _result
