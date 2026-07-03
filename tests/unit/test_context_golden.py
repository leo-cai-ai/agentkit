from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentkit.core.context.assembler import ContextAssembler
from agentkit.core.context.models import ContextRenderRequest
from agentkit.core.context.registry import ContextRegistry

GOLDEN_ROOT = Path("tests/golden/contexts")
CASES = {
    "runtime.agent-route": {
        "request.message": "FAKE-REQUEST",
        "conversation.summary": "FAKE-SUMMARY",
        "conversation.recent_messages": [
            {"role": "user", "content": "FAKE-HISTORY"}
        ],
        "routing.candidate_agents": [
            {"id": "FAKE-AGENT", "description": "FAKE-CAPABILITY"}
        ],
    },
    "runtime.intent": {
        "request.message": "FAKE-REQUEST",
        "conversation.summary": "FAKE-SUMMARY",
        "request.intent_baseline": {"goal": "FAKE-GOAL", "target": {"kind": "none"}},
    },
    "runtime.capability-route": {
        "request.message": "FAKE-REQUEST",
        "request.goal": "FAKE-GOAL",
        "routing.candidate_skills": [{"id": "FAKE-SKILL"}],
    },
    "runtime.general-answer": {
        "request.message": "FAKE-REQUEST",
        "conversation.summary": "FAKE-SUMMARY",
        "conversation.recent_messages": [
            {"role": "user", "content": "FAKE-HISTORY"}
        ],
        "routing.candidate_agents": [
            {"id": "FAKE-AGENT", "description": "FAKE-CAPABILITY"}
        ],
        "routing.decision": {"action": "answer", "reason": "FAKE-REASON"},
    },
    "runtime.react-action": {
        "request.goal": "FAKE-GOAL",
        "request.arguments": {"value": "FAKE-REQUEST"},
        "execution.allowed_tools": [{"name": "FAKE-TOOL"}],
        "execution.observations": [{"text": "FAKE-OBSERVATION"}],
        "execution.remaining_budget": {"tokens": 1000},
    },
    "runtime.plan-generate": {
        "request.goal": "FAKE-GOAL",
        "request.arguments": {"value": "FAKE-REQUEST"},
        "execution.allowed_skills": [{"id": "FAKE-SKILL"}],
        "execution.completed_artifacts": [{"id": "FAKE-ARTIFACT"}],
        "execution.previous_failure": {"reason": "FAKE-FAILURE"},
        "execution.remaining_budget": {"tokens": 1000},
    },
    "runtime.memory-extract": {
        "memory.exchange": {"user": "FAKE-REQUEST", "assistant": "FAKE-ANSWER"},
    },
    "runtime.memory-summary": {
        "memory.summary_window": {
            "existing_summary": "FAKE-SUMMARY",
            "turns": [{"role": "user", "content": "FAKE-REQUEST"}],
        },
    },
    "runtime.rag-query-rewrite": {
        "rag.query": "FAKE-REQUEST",
        "conversation.summary": "FAKE-SUMMARY",
    },
    "runtime.rag-rerank": {
        "rag.query": "FAKE-REQUEST",
        "rag.candidates": [{"id": "FAKE-1", "text": "FAKE-EVIDENCE", "score": 0.8}],
    },
    "skill.candidate-rank.summary": {
        "skill.ranking_result": {
            "job_id": "FAKE-JOB",
            "ranked_candidates": [{"id": "FAKE-CANDIDATE", "score": 90}],
        },
    },
    "skill.xhs-growth-campaign.article-generate": {
        "skill.article_evidence": [{"id": "FAKE-1", "excerpt": "FAKE-EVIDENCE"}],
        "skill.article_patterns": [{"pattern": "FAKE-PATTERN"}],
        "skill.campaign": {"topic": "FAKE-TOPIC", "language": "zh-CN"},
    },
    "skill.xhs-growth-campaign.content-review": {
        "skill.article": {"title": "FAKE-TITLE", "body": "FAKE-ARTICLE"},
        "skill.research_quality": {"status": "FAKE-QUALITY"},
        "request.language": "zh-CN",
    },
}


def render_golden(context_id: str) -> dict[str, object]:
    registry = ContextRegistry(root=Path("contexts"), tenant_selector="company_alpha")
    rendered = ContextAssembler(registry).render(
        ContextRenderRequest(
            context_id=context_id,
            tenant_id="AI-ABC",
            tenant_selector="company_alpha",
            run_id="FAKE-RUN",
            agent=SimpleNamespace(instructions="FAKE-AGENT"),
            skill=SimpleNamespace(skill_instructions="FAKE-SKILL"),
            values=CASES[context_id],
            global_token_limit=30_000,
        )
    )
    return {
        "context_id": rendered.context_id,
        "version": rendered.version,
        "system": rendered.system,
        "user": rendered.user,
        "included_inputs": list(rendered.included_inputs),
        "truncated_inputs": list(rendered.truncated_inputs),
    }


@pytest.mark.parametrize("context_id", sorted(CASES))
def test_context_render_matches_reviewed_golden(context_id: str) -> None:
    expected = json.loads(
        (GOLDEN_ROOT / f"{context_id}.json").read_text(encoding="utf-8")
    )

    assert render_golden(context_id) == expected
