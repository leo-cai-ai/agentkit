"""《红楼梦》知识图谱灌图器（Neo4j，agentkit 内置）。

连接配置优先取租户配置 ``hongloumeng`` 块（与 Web/Agent 运行时一致），
缺失时回退到环境变量 ``NEO4J_URI`` / ``NEO4J_USER`` / ``NEO4J_PASSWORD``。
通过 CLI 使用: ``agentkit hlm-seed [--command seed|clear|count] [--tenant <id>]``。

依赖: ``pip install "neo4j>=5.26,<6"``（agentkit ``kg`` extra）。
"""

from __future__ import annotations

import os
import sys
from typing import Any

from neo4j import GraphDatabase

from agentkit.core.knowledge.seed_data import (
    _SKIP_NODES,
    CHAPTERS,
    CHARACTERS,
    EVENTS,
    FAMILIES,
    OBJECTS,
    PLACES,
    RELATIONSHIPS,
)


def _connection(config: dict[str, Any] | None) -> tuple[str, str, str]:
    """解析 Neo4j 连接参数：租户配置优先，其次环境变量，最后默认值。"""
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
    uri, user, password = _connection(config)
    return GraphDatabase.driver(uri, auth=(user, password))


def _create_indexes(session: Any) -> None:
    session.run("CREATE INDEX character_name IF NOT EXISTS FOR (n:Character) ON (n.name)")
    session.run(
        "CREATE FULLTEXT INDEX character_search IF NOT EXISTS "
        "FOR (n:Character) ON EACH [n.name, n.aliases]"
    )
    session.run("CREATE INDEX place_name IF NOT EXISTS FOR (n:Place) ON (n.name)")
    session.run("CREATE INDEX event_name IF NOT EXISTS FOR (n:Event) ON (n.name)")
    session.run("CREATE INDEX object_name IF NOT EXISTS FOR (n:Object) ON (n.name)")
    session.run("CREATE INDEX family_name IF NOT EXISTS FOR (n:Family) ON (n.name)")
    session.run("CREATE INDEX chapter_name IF NOT EXISTS FOR (n:Chapter) ON (n.name)")


def _seed(session: Any) -> None:
    # ---- 家族 ----
    for name, brief in FAMILIES:
        session.run(
            "MERGE (f:Family {name: $name}) SET f.brief = $brief",
            name=name,
            brief=brief,
        )
    # ---- 人物 ----
    for name, aliases, gender, house, identity, brief, fate in CHARACTERS:
        session.run(
            """
            MERGE (c:Character {name: $name})
            SET c.aliases = $aliases, c.gender = $gender, c.house = $house,
                c.identity = $identity, c.brief = $brief, c.fate = $fate
            """,
            name=name,
            aliases=aliases,
            gender=gender,
            house=house,
            identity=identity,
            brief=brief,
            fate=fate,
        )
    # ---- 地点 / 物品 / 事件 ----
    for name, kind, brief in PLACES:
        session.run(
            "MERGE (p:Place {name: $name}) SET p.kind = $kind, p.brief = $brief",
            name=name,
            kind=kind,
            brief=brief,
        )
    for name, brief in OBJECTS:
        session.run(
            "MERGE (o:Object {name: $name}) SET o.brief = $brief",
            name=name,
            brief=brief,
        )
    for name, brief in EVENTS:
        session.run(
            "MERGE (e:Event {name: $name}) SET e.brief = $brief",
            name=name,
            brief=brief,
        )

    # ---- 章节（主要情节）----
    for no, title, summary, participants in CHAPTERS:
        name = title or f"第{no}回"
        session.run(
            "MERGE (c:Chapter {name: $name}) "
            "SET c.chapter_no = $no, c.title = $title, c.summary = $summary, "
            "c.source = $source",
            name=name,
            no=no,
            title=title,
            summary=summary,
            source="hlm_seed",
        )
        for participant in participants:
            session.run(
                "MATCH (n {name: $name}) "
                "MATCH (c:Chapter {name: $cname}) "
                "MERGE (n)-[:IN_CHAPTER]->(c)",
                name=participant,
                cname=name,
            )

    # ---- 关系（跳过未单列节点）----
    pending = [
        (src, rel, dst)
        for src, rel, dst, _props in RELATIONSHIPS
        if src not in _SKIP_NODES and dst not in _SKIP_NODES
    ]
    # 原生 MERGE 幂等写入，无需 APOC 插件
    for src, rel, dst in pending:
        session.run(
            f"MATCH (a {{name: $src}}) MATCH (b {{name: $dst}}) MERGE (a)-[r:{rel}]->(b)",
            src=src,
            dst=dst,
        )
    print(f"seed done: {len(pending)} relationships, nodes from seed_data")


def seed(config: dict[str, Any] | None = None) -> None:
    driver = _driver(config)
    try:
        with driver.session() as session:
            _create_indexes(session)
            _seed(session)
    finally:
        driver.close()


def clear(config: dict[str, Any] | None = None) -> None:
    driver = _driver(config)
    try:
        with driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
            print("graph cleared")
    finally:
        driver.close()


def count(config: dict[str, Any] | None = None) -> None:
    driver = _driver(config)
    try:
        with driver.session() as session:
            for label in (
                "Character",
                "Family",
                "Place",
                "Object",
                "Event",
                "Chapter",
            ):
                n = session.run(f"MATCH (n:{label}) RETURN count(n) AS c").single()["c"]
                print(f"{label}: {n}")
            rel = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
            print(f"relationships: {rel}")
    finally:
        driver.close()


def run(command: str = "seed", config: dict[str, Any] | None = None) -> int:
    """执行灌图命令；返回进程退出码（0 成功 / 1 失败 / 2 用法错误）。"""
    try:
        if command == "seed":
            seed(config)
        elif command == "clear":
            clear(config)
        elif command == "count":
            count(config)
        else:
            print(__doc__)
            return 2
    except Exception as exc:  # noqa: BLE001 - CLI 向用户输出安全错误摘要
        message = (str(exc).splitlines() or [type(exc).__name__])[0]
        print(f"[FAIL] {command} 失败: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
