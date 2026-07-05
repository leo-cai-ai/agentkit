from __future__ import annotations

import re

import pytest

from agentkit.connectors.xhs_text_image_cards import plan_text_image_pages


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def test_short_body_still_produces_three_pages_including_cover() -> None:
    body = "先从一个明确的问题开始。再给出一个可以今天执行的方法。"

    pages = plan_text_image_pages(title="AI 入门方法", body=body)

    assert len(pages) == 3
    assert pages[0].startswith("AI 入门方法")
    assert _compact("".join(pages[1:])) == _compact(body)


def test_medium_body_uses_dynamic_page_count() -> None:
    body = "。".join(f"第{i}条实践建议包含可执行步骤" for i in range(1, 19)) + "。"

    pages = plan_text_image_pages(
        title="企业 Agent 实践",
        body=body,
        target_chars_per_page=80,
    )

    assert 3 < len(pages) < 8
    assert _compact("".join(pages[1:])) == _compact(body)


def test_long_body_is_capped_at_eight_pages_without_losing_text() -> None:
    body = "。".join(f"第{i}段内容用于验证长正文不会被截断" for i in range(1, 80)) + "。"

    pages = plan_text_image_pages(
        title="长文测试",
        body=body,
        target_chars_per_page=60,
    )

    assert len(pages) == 8
    assert _compact("".join(pages[1:])) == _compact(body)


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(2, 8), (3, 9), (7, 6)],
)
def test_invalid_page_limits_are_rejected(minimum: int, maximum: int) -> None:
    with pytest.raises(ValueError, match="3 <= min_pages <= max_pages <= 8"):
        plan_text_image_pages(
            title="标题",
            body="正文内容足够用于测试。",
            min_pages=minimum,
            max_pages=maximum,
        )


def test_empty_title_or_body_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty title and body"):
        plan_text_image_pages(title="", body="正文")
