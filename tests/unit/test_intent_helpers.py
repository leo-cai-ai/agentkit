from agentkit.core.contracts import TaskRequest
from agentkit.core.intent import (
    detect_language,
    extract_entities,
    looks_like_business_task,
    normalize_text,
)


def test_detect_language_zh_vs_en():
    assert detect_language("你好") == "zh-CN"
    assert detect_language("hello") == "en"


def test_normalize_text_collapses_whitespace_and_lowercases():
    assert normalize_text("  Rank   THE  Top ") == "rank the top"


def test_extract_entities_from_text_when_context_empty():
    req = TaskRequest(
        user_id="u",
        roles=[],
        text="Rank candidates for JOB-001: C-100 and C-101",
    )
    entities = extract_entities(req)
    assert entities["job_id"] == "JOB-001"
    assert entities["candidate_ids"] == ["C-100", "C-101"]


def test_looks_like_business_task_detects_action_term():
    assert looks_like_business_task(text="please rank them", entities={}) is True
    assert looks_like_business_task(text="hello there", entities={}) is False
