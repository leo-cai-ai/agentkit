"""统一 SSE API 集成测试。"""

from __future__ import annotations

import json

from agentkit.core.contracts import TaskResponse
from agentkit.web.streaming import stream_response


def _frames(raw: str) -> list[tuple[str, dict]]:
    result = []
    for block in raw.split("\n\n"):
        event = "message"
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            if line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
        if data is not None:
            result.append((event, data))
    return result


def test_stream_response_emits_single_unified_final_frame() -> None:
    payload = {
        "interaction_mode": "unified",
        "agent": "customer_service",
        "strategy": "direct",
        "conversation_id": "c1",
        "run_id": "r1",
        "assistant_text": "已完成",
        "response": TaskResponse(
            status="completed",
            output={"answer": "已完成"},
            run_id="r1",
            thread_id="t1",
            agent="customer_service",
            strategy="direct",
            conversation_id="c1",
            governance={},
            audit_events=[],
        ).to_dict(),
    }
    frames = _frames("".join(stream_response(lambda: payload)))
    assert frames == [("final", payload)]
