"""小红书文字图片的确定性卡片规划。"""

from __future__ import annotations

import math
import re

PLATFORM_MAX_PAGES = 8
DEFAULT_MIN_PAGES = 3
DEFAULT_MAX_PAGES = 8
DEFAULT_TARGET_CHARS_PER_PAGE = 180

_SENTENCE_PATTERN = re.compile(r".+?(?:[。！？!?；;]+|\n+|$)", flags=re.DOTALL)


def validate_page_settings(
    *,
    min_pages: int,
    max_pages: int,
    target_chars_per_page: int,
) -> None:
    """校验文字图片的页数边界。"""

    if not 3 <= min_pages <= max_pages <= PLATFORM_MAX_PAGES:
        raise ValueError("XHS text-image pages must satisfy 3 <= min_pages <= max_pages <= 8")
    if target_chars_per_page <= 0:
        raise ValueError("XHS text-image target characters per page must be positive")


def plan_text_image_pages(
    *,
    title: str,
    body: str,
    min_pages: int = DEFAULT_MIN_PAGES,
    max_pages: int = DEFAULT_MAX_PAGES,
    target_chars_per_page: int = DEFAULT_TARGET_CHARS_PER_PAGE,
) -> list[str]:
    """根据已审核标题与正文规划封面和正文页，不生成新事实。"""

    validate_page_settings(
        min_pages=min_pages,
        max_pages=max_pages,
        target_chars_per_page=target_chars_per_page,
    )
    clean_title = " ".join(str(title).split())
    clean_body = str(body).strip()
    if not clean_title or not clean_body:
        raise ValueError("XHS text-image card planning requires non-empty title and body")

    visible_chars = _visible_length(clean_body)
    body_page_count = min(
        max_pages - 1,
        max(min_pages - 1, math.ceil(visible_chars / target_chars_per_page)),
    )
    if visible_chars < body_page_count:
        raise ValueError("XHS text-image body is too short for the configured minimum page count")

    body_pages = _balanced_pages(clean_body, body_page_count)
    hook = _first_fragment(clean_body)
    cover = clean_title if not hook else f"{clean_title}\n\n{_shorten_hook(hook)}"
    return [cover, *body_pages]


def _visible_length(value: str) -> int:
    return len(re.sub(r"\s+", "", value))


def _first_fragment(body: str) -> str:
    fragments = _semantic_fragments(body)
    return fragments[0] if fragments else ""


def _shorten_hook(value: str, *, limit: int = 72) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1].rstrip()}…"


def _semantic_fragments(body: str) -> list[str]:
    fragments = [match.group(0).strip() for match in _SENTENCE_PATTERN.finditer(body)]
    return [fragment for fragment in fragments if fragment]


def _split_at_visible_midpoint(value: str) -> tuple[str, str]:
    target = max(1, _visible_length(value) // 2)
    visible = 0
    split_at = 0
    for index, char in enumerate(value, start=1):
        if not char.isspace():
            visible += 1
        split_at = index
        if visible >= target:
            break
    left = value[:split_at].rstrip()
    right = value[split_at:].lstrip()
    if not left or not right:
        midpoint = max(1, len(value) // 2)
        left = value[:midpoint].rstrip()
        right = value[midpoint:].lstrip()
    return left, right


def _ensure_fragment_count(fragments: list[str], count: int) -> list[str]:
    result = list(fragments)
    while len(result) < count:
        index = max(range(len(result)), key=lambda item: _visible_length(result[item]))
        left, right = _split_at_visible_midpoint(result[index])
        if not left or not right:
            raise ValueError("XHS text-image body cannot be split into non-empty pages")
        result[index : index + 1] = [left, right]
    return result


def _balanced_pages(body: str, page_count: int) -> list[str]:
    fragments = _ensure_fragment_count(_semantic_fragments(body), page_count)
    pages: list[str] = []
    cursor = 0
    for page_index in range(page_count):
        remaining_pages = page_count - page_index
        if remaining_pages == 1:
            pages.append("".join(fragments[cursor:]).strip())
            break

        remaining = fragments[cursor:]
        target = math.ceil(sum(_visible_length(item) for item in remaining) / remaining_pages)
        selected: list[str] = []
        selected_chars = 0
        max_items = len(remaining) - (remaining_pages - 1)
        for fragment in remaining[:max_items]:
            fragment_chars = _visible_length(fragment)
            if selected and abs(selected_chars - target) < abs(
                selected_chars + fragment_chars - target
            ):
                break
            selected.append(fragment)
            selected_chars += fragment_chars
        pages.append("".join(selected).strip())
        cursor += len(selected)

    if len(pages) != page_count or any(not page for page in pages):
        raise ValueError("XHS text-image card planner produced an invalid page set")
    return pages
