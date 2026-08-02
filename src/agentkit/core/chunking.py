"""通用文本分块工具（供 RAG 入库、知识图谱抽取等复用）。

支持的策略（``chunk_text`` 的 ``strategy`` 参数或 ``split_*`` 直接调用）：

- ``character``：固定字符窗口滑窗切分（带 overlap），简单确定。
- ``paragraph``：按段落合并到目标长度（语义最贴合原文段落）。
- ``sentence``：按句子合并到目标长度（中英文句号都识别）。
- ``recursive``：自顶向下按「段落 → 句子 → 字符」递归切分超长片段。

所有策略返回 ``TextChunk`` 列表，带原文偏移 ``start/end``、序号与可选元数据，
便于 RAG 溯源、图谱抽取标注来源或后续对齐到原始文档。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

DEFAULT_CHUNK_SIZE = 1800
DEFAULT_CHUNK_OVERLAP = 200

# 中英文句末标点（lookbehind：切分后标点保留在前一段末尾）
SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;])")

# recursive 策略的默认分隔符层级
DEFAULT_RECURSIVE_SEPARATORS = (r"\n\s*\n", SENTENCE_BOUNDARY.pattern, "")

STRATEGIES = ("character", "paragraph", "sentence", "recursive")


@dataclass(frozen=True)
class TextChunk:
    """一段切分结果。``start/end`` 是相对源文本的字符偏移（best-effort）。"""

    text: str
    start: int = 0
    end: int = 0
    index: int = 0
    kind: str = "text"
    page: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------- 底层工具
def _split_spans(text: str, pattern: str) -> list[tuple[str, int, int]]:
    """按正则切分，返回 ``(片段, 起始, 结束)`` 三元组（片段去首尾空白，偏移仍指向原文）。"""
    spans: list[tuple[str, int, int]] = []
    cursor = 0
    for match in re.finditer(pattern, text):
        if match.start() > cursor:
            piece = text[cursor : match.start()]
            if piece.strip():
                spans.append((piece.strip(), cursor, match.start()))
        cursor = match.end()
    if cursor < len(text):
        piece = text[cursor:]
        if piece.strip():
            spans.append((piece.strip(), cursor, len(text)))
    return spans


def _merge_spans(
    spans: Sequence[tuple[str, int, int]],
    *,
    size: int,
    overlap: int,
    join: str,
    kind: str,
) -> list[TextChunk]:
    """把片段按 ``size`` 合并成块，块尾保留 ``overlap`` 字符续到下块（语义单位保持完整）。"""
    size = max(1, int(size))
    overlap = max(0, int(overlap))
    chunks: list[TextChunk] = []
    parts: list[tuple[str, int, int]] = []
    parts_len = 0

    def join_parts() -> str:
        return join.join(p[0] for p in parts).strip()

    def flush() -> None:
        text = join_parts()
        if text:
            chunks.append(
                TextChunk(
                    text=text,
                    start=parts[0][1],
                    end=parts[-1][2],
                    index=len(chunks),
                    kind=kind,
                )
            )

    for text, start, end in spans:
        add = len(text) + (len(join) if parts else 0)
        if parts and parts_len + add > size:
            flush()
            prev_end = parts[-1][2]
            if overlap:
                tail = join_parts()[-overlap:]
                parts = [(tail, max(0, prev_end - len(tail)), prev_end)]
                parts_len = len(tail)
            else:
                parts = []
                parts_len = 0
        if not parts:
            parts = [(text, start, end)]
            parts_len = len(text)
        else:
            parts.append((text, start, end))
            parts_len += len(join) + len(text)
    if parts:
        flush()
    return chunks


def _leaf_spans(
    text: str,
    separators: Sequence[str],
    max_len: int,
) -> list[tuple[str, int, int]]:
    """递归切分：先按第一个分隔符，超长片段交给下一个分隔符，最后按字符兜底。"""
    if len(text) <= max_len:
        return [(text, 0, len(text))]
    if not separators:
        return [
            (text[i : i + max_len], i, min(i + max_len, len(text)))
            for i in range(0, len(text), max_len)
        ]
    pattern = separators[0]
    if pattern:
        spans = _split_spans(text, pattern)
        if not spans:
            spans = [(text, 0, len(text))]
    else:
        spans = [
            (text[i : i + max_len], i, min(i + max_len, len(text)))
            for i in range(0, len(text), max_len)
        ]
    out: list[tuple[str, int, int]] = []
    for piece, _start, _end in spans:
        out.extend(_leaf_spans(piece, separators[1:], max_len))
    return out


# ------------------------------------------------------------- 各策略
def split_characters(
    text: str,
    *,
    size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[TextChunk]:
    """按固定字符窗口切分（滑窗，带 overlap）。"""
    size = max(1, int(size))
    overlap = max(0, int(overlap))
    step = max(1, size - overlap)
    chunks: list[TextChunk] = []
    for start in range(0, len(text), step):
        end = min(start + size, len(text))
        seg = text[start:end].strip()
        if seg:
            chunks.append(
                TextChunk(text=seg, start=start, end=end, index=len(chunks), kind="character")
            )
    return chunks


def split_paragraphs(
    text: str,
    *,
    size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    separator: str = r"\n\s*\n",
) -> list[TextChunk]:
    """按段落合并到目标长度；单个超长段落保持完整（不做硬切）。"""
    spans = _split_spans(text, separator)
    if not spans:
        spans = [(text.strip(), 0, len(text))] if text.strip() else []
    return _merge_spans(spans, size=size, overlap=overlap, join="\n", kind="paragraph")


def split_sentences(
    text: str,
    *,
    size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[TextChunk]:
    """按句子合并到目标长度（中英文句号都识别）；单个超长句子保持完整。"""
    spans = _split_spans(text, SENTENCE_BOUNDARY.pattern)
    if not spans:
        spans = [(text.strip(), 0, len(text))] if text.strip() else []
    return _merge_spans(spans, size=size, overlap=overlap, join="", kind="sentence")


def split_recursive(
    text: str,
    *,
    size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    separators: Sequence[str] | None = None,
) -> list[TextChunk]:
    """递归切分：段落 → 句子 → 字符逐级降级，再合并到目标长度。"""
    seps = list(separators) if separators else list(DEFAULT_RECURSIVE_SEPARATORS)
    spans = _leaf_spans(text, seps, size)
    return _merge_spans(spans, size=size, overlap=overlap, join="\n", kind="recursive")


# ------------------------------------------------------------- 统一入口
def chunk_text(
    text: str,
    *,
    size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    strategy: str = "paragraph",
    paragraph_separator: str = r"\n\s*\n",
) -> list[TextChunk]:
    """按指定策略切分文本。

    参数
    ----
    size : 每块目标字符数。
    overlap : 相邻块重叠字符数（语义单位内用于衔接）。
    strategy : character / paragraph / sentence / recursive 之一。
    paragraph_separator : paragraph 策略的段落分隔正则。
    """
    name = str(strategy).lower()
    if name == "character":
        return split_characters(text, size=size, overlap=overlap)
    if name == "paragraph":
        return split_paragraphs(text, size=size, overlap=overlap, separator=paragraph_separator)
    if name == "sentence":
        return split_sentences(text, size=size, overlap=overlap)
    if name == "recursive":
        return split_recursive(text, size=size, overlap=overlap)
    raise ValueError(f"未知分块策略: {strategy}; 可选: {', '.join(STRATEGIES)}")


__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_RECURSIVE_SEPARATORS",
    "SENTENCE_BOUNDARY",
    "STRATEGIES",
    "TextChunk",
    "chunk_text",
    "split_characters",
    "split_paragraphs",
    "split_recursive",
    "split_sentences",
]
