"""小红书 Workflow 在冻结内容后等待审批，恢复时只执行副作用。"""

from __future__ import annotations

import json

import agentkit.config as config_mod
import agentkit.core.llm_client as llm_client
from agentkit.core.contracts import TaskRequest
from agentkit.llm.fake import FakeProvider
from agentkit.runtime.bootstrap import build_runtime


def _responder(calls: list[str]):
    def respond(system: str, user: str) -> str:
        normalized = system.lower()
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
        if "xiaohongshu (red) growth content strategist" in normalized:
            calls.append("article")
            return (
                "TITLE: 暑假带娃旅行避坑\n"
                "BODY: 暑假带娃旅行，最难的不是选景点，而是把路程、午休和排队时间安排好。"
                "每天只安排一个重点项目，并准备下雨天可执行的室内备选。"
                "#暑假旅行 #亲子游"
            )
        if "final content-review gate" in normalized:
            calls.append("content_review")
            return json.dumps(
                {"status": "approved", "reason": "grounded", "findings": []}
            )
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
        assert waiting.output["approval"]["phase"] == "post_execution"
        assert waiting.output["approval"]["skills"] == ["xhs.growth.campaign"]
        assert waiting.output["approval"]["preview"]["title"] == "暑假带娃旅行避坑"

        resumed = runtime.gateway.resume(
            waiting.thread_id,
            approved_skills=["xhs.growth.campaign"],
        )
        assert resumed.status == "completed"
        assert resumed.output["publish"]["status"] == "published"
        assert resumed.output["publish"]["provider"] == "mock"
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
