"""小红书 Workflow 在冻结内容后等待审批，恢复时只执行副作用。"""

from __future__ import annotations

import json
from dataclasses import replace

import agentkit.config as config_mod
import agentkit.core.llm_client as llm_client
from agentkit.core.contracts import TaskRequest, TaskResponse
from agentkit.llm.fake import FakeProvider
from agentkit.runtime.bootstrap import build_runtime


def _responder(calls: list[str]):
    def respond(system: str, user: str) -> str:
        if "delegate" in system:
            return json.dumps(
                {
                    "action": "delegate",
                    "target_agent": "xhs_growth",
                    "task": "生成并发布小红书内容",
                    "reason": "属于小红书增长能力",
                    "confidence": "high",
                }
            )
        if "意图分解节点" in system:
            return json.dumps(
                {
                    "intent_type": "business_task",
                    "goal": "生成并发布小红书内容",
                    "target": {"kind": "business_skill", "name": "xhs.growth.campaign"},
                    "entities": {},
                    "confidence": "high",
                    "signals": [],
                }
            )
        if "小红书增长内容策划" in system:
            calls.append("article")
            return (
                "TITLE: 暑假带娃旅行避坑\n"
                "BODY: 暑假带娃旅行，最难的不是选景点，而是把路程、午休和排队时间安排好。"
                "每天只安排一个重点项目，并准备下雨天可执行的室内备选。"
                "#暑假旅行 #亲子游"
            )
        if "小红书内容发布前的最终审核节点" in system:
            calls.append("content_review")
            return json.dumps({"status": "approved", "reason": "grounded", "findings": []})
        raise AssertionError(f"unexpected LLM prompt: {system[:120]}")

    return respond


def _request() -> TaskRequest:
    return TaskRequest(
        user_id="growth-1",
        roles=["growth_manager"],
        text="围绕暑假带娃旅游研究小红书 Top 5，生成内容并发布",
        context={
            "agent": "xhs_growth",
            "skill": "xhs.growth.campaign",
            "topic": "暑假带娃旅游",
            "top_n": 5,
            "goal_days": 30,
            "target_followers": 10000,
            "cadence": "daily",
        },
    )


def test_xhs_approval_publishes_frozen_content(monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        llm_client,
        "_get_provider",
        lambda: FakeProvider(responder=_responder(calls)),
    )
    monkeypatch.setenv("AGENTKIT_XHS_RESEARCH_PROVIDER", "mock")
    monkeypatch.setenv("AGENTKIT_XHS_PUBLISHING_PROVIDER", "mock")
    config_mod.get_settings.cache_clear()
    try:
        runtime = build_runtime(db_path=tmp_path / "audit.sqlite")
        waiting = runtime.gateway.handle(_request())
        assert waiting.status == "waiting_for_approval"
        assert runtime.gateway.pending_approval(waiting.thread_id) is True
        assert waiting.output["approval"]["phase"] == "post_execution"
        assert waiting.output["approval"]["skills"] == ["xhs.growth.campaign"]
        assert waiting.output["approval"]["preview"]["title"] == "暑假带娃旅行避坑"
        assert waiting.output["approval"]["preview"]["card_text"]

        resumed = runtime.gateway.resume(
            waiting.thread_id,
            approved_skills=["xhs.growth.campaign"],
        )
        assert resumed.status == "completed"
        assert resumed.output["publish"]["status"] == "published"
        assert resumed.output["publish"]["provider"] == "mock"
        assert runtime.gateway.pending_approval(waiting.thread_id) is False
        assert calls.count("article") == 1
        assert calls.count("content_review") == 1
    finally:
        config_mod.get_settings.cache_clear()


def test_xhs_rejection_never_executes_publish_tool(monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        llm_client,
        "_get_provider",
        lambda: FakeProvider(responder=_responder(calls)),
    )
    monkeypatch.setenv("AGENTKIT_XHS_RESEARCH_PROVIDER", "mock")
    monkeypatch.setenv("AGENTKIT_XHS_PUBLISHING_PROVIDER", "mock")
    config_mod.get_settings.cache_clear()
    try:
        runtime = build_runtime(db_path=tmp_path / "audit-reject.sqlite")
        waiting = runtime.gateway.handle(_request())
        rejected = runtime.gateway.resume(
            waiting.thread_id,
            rejected_skills=["xhs.growth.campaign"],
        )
        assert rejected.status == "rejected"
        assert not any(
            event["type"] == "tool_finished"
            and event["payload"].get("tool") == "xhs.rpa.publish_note"
            for event in rejected.audit_events
        )
    finally:
        config_mod.get_settings.cache_clear()


def test_xhs_approval_failure_refresh_and_retry_preserve_every_visible_record(
    monkeypatch, tmp_path
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
    waiting_messages = list(attempt_1["messages"])

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
    assert refreshed["turns"][0]["user_message"] == turn["user_message"]
    assert failed["actions"][0]["status"] == "approved"
    assert failed["actions"][0]["id"] == action["id"]
    assert failed["actions"][0]["preview"] == action["preview"]
    assert failed["status"] == "failed"
    assert failed["messages"][: len(waiting_messages)] == waiting_messages
    assert failed["messages"][-1]["content"] == "发布未完成"

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
    rerun_turn = rerun["turns"][0]
    assert rerun_turn["user_message"] == turn["user_message"]
    assert len(rerun_turn["attempts"]) == 2
    assert rerun_turn["attempts"][0]["collapsed"] is True
    assert rerun_turn["attempts"][0]["messages"] == failed["messages"]
    assert rerun_turn["attempts"][0]["actions"] == failed["actions"]
    assert rerun_turn["attempts"][1]["attempt_no"] == 2
    assert rerun_turn["attempts"][1]["retry_of_attempt_id"] == failed["id"]
    assert rerun_turn["attempts"][1]["collapsed"] is False
