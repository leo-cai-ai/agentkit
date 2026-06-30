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
        if "execute-preflight node" in normalized:
            calls.append("execute_preflight")
            return json.dumps(
                {
                    "execution_goal": "prepare reviewed xhs publication",
                    "expected_outputs": ["article", "review", "publish"],
                    "risks": ["external publication"],
                }
            )
        if "xiaohongshu (red) growth content strategist" in normalized:
            calls.append("article")
            return (
                "TITLE: 暑假带娃旅行避坑\n"
                "BODY: 暑假带娃旅行，最难的不是选景点，而是把路程、午休和排队时间安排好。"
                "先按孩子年龄筛选交通半径，再确认酒店到核心景点的实际通勤时间。每天只安排一个"
                "重点项目，午后留出完整休息窗口，并准备一个下雨天也能执行的室内备选。出发前把"
                "证件、常用药和换洗衣物分成随身包与行李箱两份，临时变化时会从容很多。"
                "#暑假旅行 #亲子游 #带娃旅行"
            )
        if "final content-review gate" in normalized:
            calls.append("content_review")
            return json.dumps(
                {
                    "status": "approved",
                    "reason": "grounded and suitable for human approval",
                    "findings": [],
                }
            )
        if "output-review node" in normalized:
            calls.append("output_review")
            return json.dumps({"status": "approved", "reason": "ok", "findings": []})
        raise AssertionError(f"unexpected LLM prompt: {system[:120]}")

    return respond


def _request() -> TaskRequest:
    return TaskRequest(
        user_id="growth-1",
        roles=["growth_manager"],
        text="围绕暑假带娃旅游研究小红书 Top 5，生成内容并发布",
        context={
            "agent": "xhs_growth",
            "topic": "暑假带娃旅游",
            "top_n": 5,
            "goal_days": 30,
            "target_followers": 10000,
            "cadence": "daily",
        },
    )


def test_xhs_review_then_checkpoint_approval_publishes_frozen_content(
    monkeypatch, tmp_path
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        llm_client,
        "_get_provider",
        lambda: FakeProvider(responder=_responder(calls)),
    )
    monkeypatch.setenv("AGENTKIT_DETERMINISTIC_FASTPATH", "true")
    monkeypatch.setenv("AGENTKIT_COMBINED_INTENT_ROUTE", "false")
    monkeypatch.setenv("AGENTKIT_XHS_RESEARCH_PROVIDER", "mock")
    monkeypatch.setenv("AGENTKIT_XHS_PUBLISHING_PROVIDER", "mock")
    config_mod.get_settings.cache_clear()
    try:
        runtime = build_runtime(db_path=tmp_path / "audit.sqlite")
        waiting = runtime.gateway.handle(_request()).to_dict()

        assert waiting["output"]["status"] == "waiting_for_approval"
        approval = waiting["output"]["approval"]
        assert approval["phase"] == "post_execution"
        assert approval["skills"] == ["xhs.growth.campaign"]
        assert approval["preview"]["title"] == "暑假带娃旅行避坑"
        assert approval["preview"]["media_strategy"] == "xhs_text_image"
        assert approval["preview"]["card_style"] == "涂鸦"
        assert approval["preview"]["card_text"] == approval["preview"]["body"]
        assert "批准后将直接提交" in waiting["output"]["final"]["message"]
        assert calls.count("article") == 1
        assert calls.count("content_review") == 1

        resumed = runtime.gateway.resume(
            waiting["output"]["thread_id"],
            approved_skills=["xhs.growth.campaign"],
        ).to_dict()

        assert resumed["output"]["status"] == "published"
        assert resumed["output"]["final"]["publish"]["status"] == "published"
        assert resumed["output"]["final"]["publish"]["provider"] == "mock"
        assert resumed["output"]["final"]["metrics"]["status"] == "tracking_scheduled"
        assert resumed["output"]["governance"]["approval"]["phase"] == "post_execution"
        assert calls.count("article") == 1
        assert calls.count("content_review") == 1
        event_types = [event["type"] for event in resumed["audit_events"]]
        assert "deferred_action_started" in event_types
        assert "deferred_action_finished" in event_types
    finally:
        config_mod.get_settings.cache_clear()


def test_xhs_rejection_never_executes_publish_tool(monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        llm_client,
        "_get_provider",
        lambda: FakeProvider(responder=_responder(calls)),
    )
    monkeypatch.setenv("AGENTKIT_DETERMINISTIC_FASTPATH", "true")
    monkeypatch.setenv("AGENTKIT_XHS_RESEARCH_PROVIDER", "mock")
    monkeypatch.setenv("AGENTKIT_XHS_PUBLISHING_PROVIDER", "mock")
    config_mod.get_settings.cache_clear()
    try:
        runtime = build_runtime(db_path=tmp_path / "audit-reject.sqlite")
        waiting = runtime.gateway.handle(_request()).to_dict()
        rejected = runtime.gateway.resume(
            waiting["output"]["thread_id"],
            rejected_skills=["xhs.growth.campaign"],
        ).to_dict()

        assert rejected["output"]["status"] == "rejected"
        event_types = [event["type"] for event in rejected["audit_events"]]
        assert "deferred_action_started" not in event_types
        assert calls.count("article") == 1
    finally:
        config_mod.get_settings.cache_clear()
