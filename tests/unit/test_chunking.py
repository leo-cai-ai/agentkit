from __future__ import annotations

import pytest

from agentkit.core.chunking import (
    TextChunk,
    chunk_text,
    split_characters,
    split_paragraphs,
    split_recursive,
    split_sentences,
)


def test_character_chunker_respects_size_and_overlap():
    text = "a" * 100
    chunks = split_characters(text, size=30, overlap=10)

    assert chunks
    assert all(len(c.text) <= 30 for c in chunks)
    # 滑窗 overlap：后一块开头等于前一块末尾
    assert chunks[0].text[-10:] == chunks[1].text[:10]


def test_character_chunker_empty_text_returns_empty():
    assert split_characters("", size=30, overlap=0) == []


def test_paragraph_chunker_merges_whole_paragraphs():
    text = "\n\n".join(f"段落{i}" for i in range(12))
    chunks = split_paragraphs(text, size=20, overlap=0)

    assert chunks
    # 每块都应是完整段落合并而来，且不超过 size（段落本身均小于 size）
    assert all(len(c.text) <= 20 for c in chunks)
    assert all("段落" in c.text for c in chunks)
    # 每段 3 字符 + 换行 1 字符，size=20 约每块 5 段
    assert chunks[0].text == "段落0\n段落1\n段落2\n段落3\n段落4"
    assert len(chunks) == 3


def test_paragraph_chunker_keeps_oversized_paragraph_whole():
    big = "长" * 500
    chunks = split_paragraphs(big, size=100, overlap=0)
    assert len(chunks) == 1
    assert chunks[0].text == big


def test_sentence_chunker_splits_chinese_and_english():
    text = "你好。世界！这是测试；It works. Next!"
    chunks = split_sentences(text, size=500, overlap=0)

    assert len(chunks) == 1
    assert "。" in chunks[0].text
    assert ". " in chunks[0].text or ".Next" in chunks[0].text or ". Next" in chunks[0].text


def test_sentence_chunker_respects_size():
    text = "这是第一句。" * 20
    chunks = split_sentences(text, size=25, overlap=0)

    assert len(chunks) > 1
    assert all(len(c.text) <= 25 for c in chunks)
    # 句子为单位：每块以句号结尾（除可能的最末块）
    assert all(c.text.endswith("。") for c in chunks)


def test_recursive_chunker_falls_back_to_chars():
    text = "字" * 500
    chunks = split_recursive(text, size=100, overlap=0)

    assert len(chunks) >= 5
    assert all(len(c.text) <= 100 for c in chunks)


def test_chunk_text_dispatch_matches_specialized():
    text = "第一段。\n\n第二段。\n\n第三段。"
    assert chunk_text(text, size=30, strategy="paragraph") == split_paragraphs(
        text, size=30, overlap=200
    )


def test_chunk_text_unknown_strategy_raises():
    with pytest.raises(ValueError):
        chunk_text("abc", strategy="nope")


def test_offsets_are_in_bounds_for_paragraphs():
    text = "词。\n\n" * 30
    for chunk in split_paragraphs(text, size=40, overlap=5):
        assert isinstance(chunk, TextChunk)
        assert 0 <= chunk.start <= chunk.end <= len(text)


def test_offsets_are_in_bounds_for_sentences():
    text = "第一句。第二句！第三句？"
    for chunk in split_sentences(text, size=8, overlap=0):
        assert 0 <= chunk.start <= chunk.end <= len(text)
