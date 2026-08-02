"""通用 PDF → Neo4j 知识图谱流水线（agentkit 内置）。

读取任意 PDF → 按段落分块 → LLM 抽取实体/关系（受 schema 白名单约束）→ MERGE 入库。
每个知识库对应租户配置里的一个 ``<kg>`` 配置块（含 Neo4j 连接 + 可选 schema），
因此可以给不同主题的 PDF 灌进各自的知识图库。

用法::

    agentkit kg-ingest <pdf> --tenant <id> --kg <kg_name> \
        [--clear] [--json] [--max-chunks N] [--chunk-size N] \
        [--chunk-overlap N] [--chunk-strategy paragraph|sentence|character|recursive]

schema（允许的节点标签 / 关系类型）默认使用通用值；如需与某领域一致（如红楼梦的
Character/Place 等标签），在租户配置对应 ``<kg>`` 块里加 ``"schema": {...}`` 覆盖。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any

from agentkit.core.chunking import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
)
from agentkit.core.chunking import (
    chunk_text as _generic_chunk_text,
)
from agentkit.core.llm_client import require_chat_json

# 默认通用 schema（可用租户配置的 schema 覆盖）
DEFAULT_SCHEMA: dict[str, Any] = {
    "node_labels": [
        "Person",
        "Organization",
        "Place",
        "Object",
        "Event",
        "Concept",
        "Chapter",
    ],
    "rel_types": [
        "FATHER_OF",
        "MOTHER_OF",
        "SPOUSE_OF",
        "SIBLING_OF",
        "CHILD_OF",
        "PARENT_OF",
        "FRIEND_OF",
        "SERVES",
        "OWNS",
        "BELONGS_TO",
        "LIVES_AT",
        "APPEARS_IN",
        "LOCATED_IN",
        "WORKED_AT",
        "FOUNDED",
        "PART_OF",
        "RELATED_TO",
        "MENTIONS",
        "IN_CHAPTER",
    ],
}

_MAX_EXTRACTED_PER_CHUNK = 40


def _max_entities_for_chunk(chunk_len: int) -> int:
    """块越大允许返回越多实体；约每 45 字符 1 个，下限 _MAX_EXTRACTED_PER_CHUNK、上限 100。"""
    return max(_MAX_EXTRACTED_PER_CHUNK, min(100, chunk_len // 45))


@dataclass
class IngestReport:
    """一次入库的结果统计。"""

    chunks: int = 0
    entities: int = 0
    chapters: int = 0
    relationships_created: int = 0
    relationships_skipped: int = 0
    chunks_failed: int = 0
    llm_seconds: float = 0.0
    db_seconds: float = 0.0
    total_seconds: float = 0.0


# ------------------------------------------------------------- 连接参数
def _connection(config: dict[str, Any] | None) -> tuple[str, str, str]:
    block = config or {}
    uri = (
        block.get("neo4j_uri")
        or os.environ.get("NEO4J_URI")
        or "neo4j://localhost:7687"
    )
    user = block.get("neo4j_user") or os.environ.get("NEO4J_USER") or "neo4j"
    password = (
        block.get("neo4j_password")
        or os.environ.get("NEO4J_PASSWORD")
        or "neo4j123"
    )
    return uri, user, password


def _driver(config: dict[str, Any] | None) -> Any:
    from neo4j import GraphDatabase

    uri, user, password = _connection(config)
    return GraphDatabase.driver(uri, auth=(user, password))


# ------------------------------------------------------------- 读取 / 切块
# 从第几页开始读（start_page，1-based），用来去掉前言、出版社等不相关的数据
def read_pdf(path: str, start_page: int = 0, max_pages: int = 0) -> str:
    """提取 PDF 全文（逐页拼接，可按页数/起始页截断）。

    底层走通用提取器 ``agentkit.core.pdf_extract``；如需表格/图片/OCR 块，直接使用该模块。
    """
    from agentkit.core.pdf_extract import extract_pdf

    result = extract_pdf(path, start_page=start_page, max_pages=max_pages)
    if not result.text.strip():
        raise ValueError(f"PDF 未提取到任何文本: {path}")
    return result.text


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    strategy: str = "paragraph",
) -> list[str]:
    """把文本切成块（默认段落合并，等价于原实现）。

    ``strategy`` 可指定 character / paragraph / sentence / recursive，
    底层复用 ``agentkit.core.chunking``，供 RAG 入库与图谱抽取共用。
    """
    return [
        item.text
        for item in _generic_chunk_text(
            text,
            size=chunk_size,
            overlap=chunk_overlap,
            strategy=strategy,
            paragraph_separator=r"\n{1,}",
        )
    ]


# ------------------------------------------------------------- LLM 抽取
def _extraction_prompt(
    chunk: str,
    schema: dict[str, Any],
    source: str,
    max_entities: int,
) -> tuple[str, str]:
    # Chapter 是结构节点（挂实体用），不作为可抽取实体标签，避免与实体混淆
    entity_labels = [label for label in schema["node_labels"] if label != "Chapter"]
    labels = ", ".join(entity_labels)
    rel_types = ", ".join(schema["rel_types"])
    system = (
        "你是知识图谱信息抽取引擎，严格依据给定文本抽取实体与关系。\n"
        "规则：\n"
        f"1. 实体标签只能是以下之一：{labels}。\n"
        f"2. 关系类型只能是以下之一：{rel_types}。\n"
        "3. 只抽取文本中明确出现、或可由上下文可靠推出的实体/关系；不要臆造。\n"
        "4. 返回严格 JSON，格式："
        '{"entities":[{"name":"","label":"","aliases":[],"brief":""}],'
        '"relationships":[{"source":"","relation":"","target":""}],'
        '"chapter":{"no":0,"title":"","summary":"","participants":[]}}\n'
        "5. 实体 name 用最常用名称；aliases 填文本中出现的别名/称号（可为空数组）。\n"
        "6. relationships 的 source/target 必须能在本次返回的 entities 中找到同名实体。\n"
        f"7. 最多返回 {max_entities} 个实体；宁可少而准，不要多而错。\n"
        "8. 若文本可确定所属章节/回目（出现‘第X回’或回目标题），在 chapter 中填写："
        "no(回目序号，整数)、title(回目/章节标题)、summary(本章主要情节，2-3 句话，"
        "写清发生了什么事、谁做了什么)；无法确定时 chapter 返回 null。\n"
        "9. chapter.participants 列出本段/本章出现的关键实体名（需与 entities 的 name "
        "一致），用于把实体挂到对应章节；可为空数组。"
    )
    user = f"来源：{source}\n\n文本：\n{chunk}"
    return system, user


def _extract_chunk(
    chunk: str,
    schema: dict[str, Any],
    source: str,
    max_entities: int,
) -> dict[str, Any]:
    system, user = _extraction_prompt(chunk, schema, source, max_entities)
    data = require_chat_json(system, user)
    if not isinstance(data, dict):
        raise ValueError("LLM 未返回 JSON 对象")
    return data


def _normalize(
    data: dict[str, Any],
    schema: dict[str, Any],
    max_entities: int,
) -> tuple[dict[str, Any], list[tuple[str, str, str]], dict[str, Any] | None]:
    """把一次抽取结果规整为 (entities_by_key, relationships, chapter)，只保留白名单内条目。"""
    allowed_labels = set(schema["node_labels"])
    allowed_rels = set(schema["rel_types"])
    # Chapter 是结构节点，不参与实体抽取
    entity_labels = {label for label in allowed_labels if label != "Chapter"}
    entities: dict[str, Any] = {}
    for raw in (data.get("entities") or [])[:max_entities]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        label = str(raw.get("label") or "").strip()
        if not name or label not in entity_labels:
            continue
        key = f"{label}::{name}"
        if key not in entities:
            aliases = [str(a).strip() for a in (raw.get("aliases") or []) if str(a).strip()]
            entities[key] = {
                "name": name,
                "label": label,
                "aliases": aliases,
                "brief": str(raw.get("brief") or "").strip(),
            }
    relationships: list[tuple[str, str, str]] = []
    for raw in data.get("relationships") or []:
        if not isinstance(raw, dict):
            continue
        src = str(raw.get("source") or "").strip()
        tgt = str(raw.get("target") or "").strip()
        rel = str(raw.get("relation") or "").strip()
        if not src or not tgt or rel not in allowed_rels:
            continue
        relationships.append((src, rel, tgt))
    return entities, relationships, _normalize_chapter(data, entities)


def _normalize_chapter(
    data: dict[str, Any],
    entities: dict[str, Any],
) -> dict[str, Any] | None:
    """解析可选章节信息；只保留本块确实出现的参与者。"""
    raw = data.get("chapter")
    if not isinstance(raw, dict):
        return None
    no_raw = raw.get("no")
    no: int | None = None
    if isinstance(no_raw, int) and no_raw > 0:
        no = no_raw
    elif isinstance(no_raw, str) and no_raw.strip().isdigit():
        no = int(no_raw.strip())
    title = str(raw.get("title") or "").strip()
    summary = str(raw.get("summary") or "").strip()
    if no is None and not title and not summary:
        return None
    known = {ent["name"] for ent in entities.values()}
    participants = [
        str(p).strip()
        for p in (raw.get("participants") or [])
        if str(p).strip() in known
    ]
    return {"no": no, "title": title, "summary": summary, "participants": participants}


def _chapter_name(chapter: dict[str, Any]) -> str:
    """章节节点的唯一键：优先回目标题，否则用‘第N回’。"""
    title = chapter.get("title") or ""
    if title:
        return title
    no = chapter.get("no")
    if no is not None:
        return f"第{no}回"
    return ""


# ------------------------------------------------------------- 入库
def _ensure_schema(driver: Any, schema: dict[str, Any]) -> None:
    """为每个节点标签建 name 索引 + (name, aliases) 全文索引（幂等）。"""
    with driver.session() as session:
        for label in schema["node_labels"]:
            safe = re.sub(r"[^A-Za-z0-9_]", "_", label).lower()
            session.run(
                f"CREATE INDEX {safe}_name IF NOT EXISTS FOR (n:{label}) ON (n.name)"
            )
            session.run(
                f"CREATE FULLTEXT INDEX {safe}_search IF NOT EXISTS "
                f"FOR (n:{label}) ON EACH [n.name, n.aliases]"
            )


def _merge(
    driver: Any,
    entities: dict[str, Any],
    relationships: list[tuple[str, str, str]],
    chapters: list[dict[str, Any]] | None = None,
    source: str = "",
) -> tuple[int, int]:
    """MERGE 实体/关系/章节（幂等）。返回 (创建的关系数, 因端点缺失被跳过的关系数)。"""
    made = 0
    skipped = 0
    name_to_label: dict[str, str] = {}
    for ent in entities.values():
        name_to_label.setdefault(ent["name"], ent["label"])
    with driver.session() as session:
        for ent in entities.values():
            session.run(
                f"MERGE (n:{ent['label']} {{name: $name}}) "
                "SET n.aliases = $aliases, n.brief = $brief",
                name=ent["name"],
                aliases=ent["aliases"],
                brief=ent["brief"],
            )
        for src, rel, tgt in relationships:
            src_label = name_to_label.get(src)
            tgt_label = name_to_label.get(tgt)
            if not src_label or not tgt_label:
                skipped += 1
                continue
            session.run(
                f"MATCH (a:{src_label} {{name: $src}}) "
                f"MATCH (b:{tgt_label} {{name: $tgt}}) "
                f"MERGE (a)-[r:{rel}]->(b)",
                src=src,
                tgt=tgt,
            )
            made += 1
        for chapter in chapters or []:
            made += _merge_chapter(session, chapter, name_to_label, source)
    return made, skipped


def _merge_chapter(
    session: Any,
    chapter: dict[str, Any],
    name_to_label: dict[str, str],
    source: str,
) -> int:
    """创建/更新 Chapter 节点并挂接参与实体；返回新增的 IN_CHAPTER 关系数。"""
    name = _chapter_name(chapter)
    if not name:
        return 0
    session.run(
        "MERGE (c:Chapter {name: $name}) "
        "SET c.chapter_no = $no, c.title = $title, c.summary = $summary, "
        "c.source = $source",
        name=name,
        no=chapter.get("no"),
        title=chapter.get("title") or "",
        summary=chapter.get("summary") or "",
        source=source,
    )
    made = 0
    for participant in chapter.get("participants") or []:
        label = name_to_label.get(participant)
        if not label:
            continue
        session.run(
            f"MATCH (n:{label} {{name: $name}}) "
            "MATCH (c:Chapter {name: $cname}) "
            "MERGE (n)-[:IN_CHAPTER]->(c)",
            name=participant,
            cname=name,
        )
        made += 1
    return made


# ------------------------------------------------------------- 编排
def run_ingest(
    pdf_path: str,
    kg_config: dict[str, Any] | None,
    *,
    clear: bool = False,
    max_chunks: int = 0,
    start_page: int = 0,
    max_pages: int = 0,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    chunk_strategy: str = "paragraph",
) -> IngestReport:
    """完整流水线：读 PDF → 分块 → LLM 抽取 → MERGE 入库。"""
    started = time.perf_counter()
    schema = (kg_config or {}).get("schema") or DEFAULT_SCHEMA
    source = os.path.basename(pdf_path)
    text = read_pdf(pdf_path, start_page=start_page, max_pages=max_pages)
    read_seconds = time.perf_counter() - started
    chunks = chunk_text(
        text,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        strategy=chunk_strategy,
    )
    if max_chunks and max_chunks > 0:
        chunks = chunks[:max_chunks]
    chunk_seconds = time.perf_counter() - started - read_seconds
    print(
        f"[timing] 读取PDF={read_seconds:.2f}s 切块={chunk_seconds:.3f}s "
        f"共{len(chunks)}块（每块目标{chunk_size}字）"
    )

    driver = _driver(kg_config)
    report = IngestReport(chunks=len(chunks))
    try:
        if clear:
            print(
                "[warn] --clear 将清空整张图（包括已有全部节点与关系）。"
                + (
                    f"注意：当前搭配 --start-page {start_page}，起始页之前的数据也会被一并删除；"
                    "如需保留请去掉 --clear 增量入库。"
                    if start_page > 0
                    else "如非全量重建请去掉 --clear。"
                )
            )
            with driver.session() as session:
                session.run("MATCH (n) DETACH DELETE n")
        schema_started = time.perf_counter()
        _ensure_schema(driver, schema)
        schema_seconds = time.perf_counter() - schema_started
        if schema_seconds > 0.05:
            print(f"[timing] 建索引/校验schema={schema_seconds:.2f}s")

        all_entities: dict[str, Any] = {}
        all_relationships: list[tuple[str, str, str]] = []
        chapter_map: dict[str, dict[str, Any]] = {}
        llm_total = 0.0
        for index, chunk in enumerate(chunks, start=1):
            chunk_started = time.perf_counter()
            llm_seconds = 0.0
            try:
                max_entities = _max_entities_for_chunk(len(chunk))
                llm_started = time.perf_counter()
                data = _extract_chunk(chunk, schema, source, max_entities)
                llm_seconds = time.perf_counter() - llm_started
                llm_total += llm_seconds
                entities, relationships, chapter = _normalize(data, schema, max_entities)
            except Exception as exc:  # noqa: BLE001 - 单块失败不影响整体
                report.chunks_failed += 1
                llm_total += llm_seconds
                message = (str(exc).splitlines() or [type(exc).__name__])[0]
                print(
                    f"[timing] chunk {index}/{len(chunks)} · {len(chunk)}字 · "
                    f"llm={llm_seconds:.1f}s · 失败: {message}"
                )
                continue
            normalize_seconds = time.perf_counter() - chunk_started - llm_seconds
            print(
                f"[timing] chunk {index}/{len(chunks)} · {len(chunk)}字 · "
                f"llm={llm_seconds:.1f}s · 规整={normalize_seconds:.3f}s"
            )
            for key, ent in entities.items():
                if key not in all_entities:
                    all_entities[key] = ent
                else:
                    existing = all_entities[key]
                    existing["aliases"] = list(
                        dict.fromkeys(existing["aliases"] + ent["aliases"])
                    )
                    if ent["brief"] and not existing["brief"]:
                        existing["brief"] = ent["brief"]
            all_relationships.extend(relationships)
            if chapter:
                cname = _chapter_name(chapter)
                if cname:
                    if cname in chapter_map:
                        existing = chapter_map[cname]
                        existing["participants"] = sorted(
                            set(existing["participants"] + chapter["participants"])
                        )
                        if not existing["summary"] and chapter["summary"]:
                            existing["summary"] = chapter["summary"]
                    else:
                        chapter_map[cname] = chapter

        report.entities = len(all_entities)
        report.chapters = len(chapter_map)
        db_started = time.perf_counter()
        report.relationships_created, report.relationships_skipped = _merge(
            driver,
            all_entities,
            all_relationships,
            list(chapter_map.values()),
            source=source,
        )
        db_seconds = time.perf_counter() - db_started
        total_seconds = time.perf_counter() - started
        report.llm_seconds = round(llm_total, 1)
        report.db_seconds = round(db_seconds, 1)
        report.total_seconds = round(total_seconds, 1)
        llm_pct = (llm_total / total_seconds * 100) if total_seconds > 0 else 0.0
        db_pct = (db_seconds / total_seconds * 100) if total_seconds > 0 else 0.0
        print(
            f"[timing] 汇总: LLM合计={llm_total:.1f}s({llm_pct:.0f}%) "
            f"入库MERGE={db_seconds:.1f}s({db_pct:.0f}%) "
            f"总耗时={total_seconds:.1f}s"
        )
        if llm_pct > 70:
            print(
                "[timing] 瓶颈在 LLM 生成：可调小 --chunk-size/减少单块实体数，"
                "或换更快模型以缩短单次生成时间。入库(MERGE)基本不占时间。"
            )
        return report
    finally:
        driver.close()


__all__ = [
    "DEFAULT_SCHEMA",
    "IngestReport",
    "chunk_text",
    "read_pdf",
    "run_ingest",
]
