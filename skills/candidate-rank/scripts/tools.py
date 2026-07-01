"""候选人排序 Skill 使用的 ATS 工具适配。"""

from __future__ import annotations

from agentkit.connectors.mock_ats import MockAtsConnector

_ATS = MockAtsConnector()


def get_job(args: dict) -> dict:
    """获取一个职位需求。"""
    return _ATS.get_job(args["job_id"])


def get_candidates(args: dict) -> dict:
    """获取一组候选人资料。"""
    return {"candidates": _ATS.get_candidates(args["candidate_ids"])}
