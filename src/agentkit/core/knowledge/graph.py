"""《红楼梦》Neo4j 知识图谱问答客户端。

被 hongloumeng skill 的 tool/handler 与 Web 图谱可视化页面共用：
- :meth:`HlmKgClient.graph_snapshot`   —— 供可视化页面导出节点/边
- :meth:`HlmKgClient.search_entities` —— 供页面/问答做实体搜索
- :meth:`HlmKgClient.ask`             —— 端到端问答（实体解析 -> Text2Cypher
  -> 图谱检索 -> LLM 合成回答），含只读 Cypher 保护与邻域查询兜底
"""

from __future__ import annotations

import json
import re
from typing import Any

from neo4j import GraphDatabase

from agentkit.core.llm_client import require_chat, require_chat_json

# 只读保护：仅允许 SELECT 类查询，拒绝任何写/DDL/过程调用
_BLOCKED_KEYWORDS = re.compile(
    r"\b(create|merge|delete|detach|set|remove|drop|foreach|load\s+csv|"
    r"create\s+index|create\s+constraint|db\.|call|algo\.|gds\.)\b",
    re.IGNORECASE,
)
_ALLOWED_STARTS = ("MATCH", "WITH", "UNWIND", "OPTIONAL MATCH", "RETURN")
_MAX_ROWS = 200
_REL_LABEL = {
    "FATHER_OF": "父亲",
    "MOTHER_OF": "母亲",
    "SPOUSE_OF": "配偶",
    "SIBLING_OF": "兄弟姐妹",
    "COUSIN_OF": "表/堂亲",
    "AUNT_OF": "姑姨",
    "GRANDMOTHER_OF": "祖母/外祖母",
    "LOVES": "情缘",
    "SERVES": "服侍",
    "FRIEND_OF": "好友",
    "BELONGS_TO": "属于",
    "LIVES_AT": "居于",
    "OWNS": "持有",
    "APPEARS_IN": "参与事件",
    "LOCATED_IN": "位于",
}


def _assert_read_only(cypher: str) -> None:
    statement = cypher.strip()
    if not statement or ";" in statement:
        raise ValueError("只允许单条只读查询")
    if _BLOCKED_KEYWORDS.search(statement):
        raise ValueError("查询包含被禁止的写/DDL 语句")
    if not statement.upper().startswith(_ALLOWED_STARTS):
        raise ValueError("查询必须以 MATCH/WITH/RETURN 等只读子句开头")


class HlmKgClient:
    """Neo4j 知识图谱问答客户端（懒连接，只读）。"""

    def __init__(
        self,
        *,
        uri: str,
        user: str,
        password: str,
        max_rows: int = _MAX_ROWS,
        neighborhood_depth: int = 2,
        neighborhood_limit: int = 40,
    ) -> None:
        self._uri = uri
        self._user = user
        self._password = password
        self._max_rows = int(max_rows)
        self._depth = int(neighborhood_depth)
        self._limit = int(neighborhood_limit)
        self._driver: Any = None

    # ------------------------------------------------------------------ 连接
    def _get_driver(self) -> Any:
        if self._driver is None:
            self._driver = GraphDatabase.driver(
                self._uri,
                auth=(self._user, self._password),
            )
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def __enter__(self) -> HlmKgClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------- 图谱快照
    def graph_snapshot(self, *, limit: int = 120) -> dict[str, Any]:
        """导出图谱节点与边，供可视化页面渲染。

        为避免“任意取 N 个节点 + 任意取 N 条边”导致节点孤立、关系缺失、事件被
        漏掉，这里改为连通子图取样：先按度数取前若干种子节点（并专门带上若干高连
        度的 Event），再取它们的 1 跳邻居，最后只返回这些节点之间的边——保证可见
        子图连通、人物/地点/事件的关系可读。
        """
        limit = max(1, int(limit))
        seed = max(8, min(40, limit // 3))
        events = max(4, min(12, limit // 6))
        with self._get_driver().session() as session:
            seed_rows = session.run(
                """
                MATCH (n)
                WHERE (n:Character OR n:Place OR n:Object OR n:Event OR n:Family)
                WITH n, count { (n)--() } AS degree
                ORDER BY degree DESC
                LIMIT $seed
                RETURN n.name AS name
                """,
                seed=seed,
            ).data()
            event_rows = session.run(
                """
                MATCH (e:Event)
                WITH e, count { (e)--() } AS degree
                ORDER BY degree DESC
                LIMIT $events
                RETURN e.name AS name
                """,
                events=events,
            ).data()

        node_names: list[str] = []
        for row in [*seed_rows, *event_rows]:
            name = row.get("name")
            if name and name not in node_names:
                node_names.append(name)
            if len(node_names) >= limit:
                break

        if node_names:
            with self._get_driver().session() as session:
                neighbor_rows = session.run(
                    """
                    MATCH (a)-[r]-(b)
                    WHERE a.name IN $names
                      AND (b:Character OR b:Place OR b:Object OR b:Event OR b:Family)
                    RETURN b.name AS name
                    """,
                    names=node_names,
                ).data()
            for row in neighbor_rows:
                name = row.get("name")
                if name and name not in node_names:
                    node_names.append(name)
                if len(node_names) >= limit:
                    break

        if not node_names:
            return {"nodes": [], "edges": []}

        with self._get_driver().session() as session:
            nodes_rows = session.run(
                """
                MATCH (n)
                WHERE n.name IN $names
                RETURN n.name AS name, labels(n)[0] AS label,
                       coalesce(n.identity, n.kind, n.brief, '') AS info
                """,
                names=node_names,
            ).data()
            edges_rows = session.run(
                """
                MATCH (a)-[r]->(b)
                WHERE a.name IN $names AND b.name IN $names
                RETURN a.name AS source, type(r) AS relation, b.name AS target
                """,
                names=node_names,
            ).data()
        seen_nodes: set[str] = set()
        nodes: list[dict[str, Any]] = []
        for row in nodes_rows:
            name = row.get("name")
            if not name or name in seen_nodes:  # 同名多标签去重，避免前端 id 冲突
                continue
            seen_nodes.add(name)
            nodes.append(
                {
                    "id": name,
                    "label": row.get("label") or "",
                    "info": str(row.get("info") or ""),
                }
            )
        seen_edges: set[tuple[str, str, str]] = set()
        edges: list[dict[str, Any]] = []
        for row in edges_rows:
            source = row.get("source")
            target = row.get("target")
            if not source or not target:
                continue
            key = (source, str(row.get("relation") or ""), target)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append(
                {
                    "source": source,
                    "relation": row.get("relation"),
                    "target": target,
                }
            )
        return {"nodes": nodes, "edges": edges}

    def search_entities(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """按名称/别名模糊匹配人物实体。"""
        q = str(query or "").strip()
        if not q:
            return []
        with self._get_driver().session() as session:
            rows = session.run(
                """
                MATCH (c:Character)
                RETURN c.name AS name, c.aliases AS aliases, c.identity AS identity,
                       c.brief AS brief, c.fate AS fate
                """
            ).data()
        matches = []
        for row in rows:
            name = str(row.get("name") or "")
            aliases = list(row.get("aliases") or [])
            if q in name or q in " ".join(aliases):
                matches.append(
                    {
                        "name": name,
                        "aliases": aliases,
                        "identity": str(row.get("identity") or ""),
                        "brief": str(row.get("brief") or ""),
                        "fate": str(row.get("fate") or ""),
                    }
                )
            if len(matches) >= limit:
                break
        return matches

    # ------------------------------------------------------------- 详情/情节
    def entity_detail(self, name: str, *, chapter_limit: int = 12) -> dict[str, Any]:
        """返回实体基本信息及其出现章节的主要情节（供页面点开节点查看）。"""
        name = str(name or "").strip()
        if not name:
            return {"name": "", "label": "", "info": "", "chapters": [], "chapter_count": 0}
        with self._get_driver().session() as session:
            rows = session.run(
                """
                MATCH (n {name: $name})
                WITH n LIMIT 1
                OPTIONAL MATCH (n)-[:IN_CHAPTER]->(c:Chapter)
                RETURN n.name AS name, labels(n)[0] AS label,
                       coalesce(n.identity, n.kind, n.brief, '') AS info,
                       n.brief AS brief,
                       collect(DISTINCT {
                           name: c.name, no: c.chapter_no, summary: c.summary
                       }) AS chapters
                """,
                name=name,
            ).data()
        if not rows:
            return {"name": name, "label": "", "info": "", "chapters": [], "chapter_count": 0}
        row = rows[0]
        chapters = self._sort_chapters(row.get("chapters") or [])
        return {
            "name": str(row.get("name") or name),
            "label": str(row.get("label") or ""),
            "info": str(row.get("info") or ""),
            "brief": str(row.get("brief") or ""),
            "chapters": chapters[:chapter_limit],
            "chapter_count": len(chapters),
        }

    def edge_detail(
        self,
        source: str,
        relation: str,
        target: str,
        *,
        chapter_limit: int = 12,
    ) -> dict[str, Any]:
        """返回两点之间指定关系的详情，以及两人共同出现的章节情节。"""
        source = str(source or "").strip()
        target = str(target or "").strip()
        relation = str(relation or "").strip()
        if not re.fullmatch(r"[A-Z_]+", relation):
            return {
                "source": source,
                "relation": relation,
                "target": target,
                "chapters": [],
                "chapter_count": 0,
            }
        with self._get_driver().session() as session:
            rel_rows = session.run(
                f"""
                MATCH (a {{name: $source}})-[r:{relation}]->(b {{name: $target}})
                RETURN a.brief AS source_brief, b.brief AS target_brief
                LIMIT 1
                """,
                source=source,
                target=target,
            ).data()
            chapter_rows = session.run(
                """
                MATCH (a {name: $source})-[:IN_CHAPTER]->(c:Chapter)
                      <-[:IN_CHAPTER]-(b {name: $target})
                RETURN c.name AS name, c.chapter_no AS no, c.summary AS summary
                """,
                source=source,
                target=target,
            ).data()
        chapters = self._sort_chapters(chapter_rows)
        rel = rel_rows[0] if rel_rows else {}
        return {
            "source": source,
            "relation": relation,
            "target": target,
            "source_brief": str(rel.get("source_brief") or ""),
            "target_brief": str(rel.get("target_brief") or ""),
            "chapters": chapters[:chapter_limit],
            "chapter_count": len(chapters),
        }

    def path_between(
        self,
        source: str,
        target: str,
        *,
        max_depth: int = 6,
        limit: int = 3,
    ) -> dict[str, Any]:
        """返回两点之间的直接关系与最短路径（供双节点多选时查看它们的关系）。"""
        source = str(source or "").strip()
        target = str(target or "").strip()
        if not source or not target:
            return {"source": source, "target": target, "direct": [], "paths": []}
        with self._get_driver().session() as session:
            direct_rows = session.run(
                """
                MATCH (a {name: $s})-[r]->(b {name: $t})
                RETURN type(r) AS relation,
                       a.brief AS source_brief, b.brief AS target_brief
                """,
                s=source,
                t=target,
            ).data()
            # shortestPath 不支持参数化最大深度，需内联字面量（depth 已按 int 约束）
            depth = max(1, min(int(max_depth), 10))
            path_rows = session.run(
                f"""
                MATCH p = shortestPath((a {{name: $s}})-[*..{depth}]-(b {{name: $t}}))
                RETURN [n IN nodes(p) | n.name] AS nodes,
                       [r IN relationships(p) | type(r)] AS rels
                LIMIT $limit
                """,
                s=source,
                t=target,
                limit=int(limit),
            ).data()
        return {
            "source": source,
            "target": target,
            "direct": [
                {
                    "relation": row["relation"],
                    "source_brief": str(row.get("source_brief") or ""),
                    "target_brief": str(row.get("target_brief") or ""),
                }
                for row in direct_rows
                if row.get("relation")
            ],
            "paths": [
                {
                    "nodes": [str(n) for n in (row.get("nodes") or [])],
                    "rels": [str(r) for r in (row.get("rels") or [])],
                }
                for row in path_rows
                if row.get("nodes")
            ],
        }

    @staticmethod
    def _sort_chapters(raw: list[Any]) -> list[dict[str, Any]]:
        """把 Cypher 返回的章节行规整并按回目排序。"""
        chapters = [
            {
                "name": str(item.get("name") or ""),
                "no": item.get("no"),
                "summary": str(item.get("summary") or ""),
            }
            for item in raw
            if isinstance(item, dict) and item.get("name")
        ]
        chapters.sort(
            key=lambda c: (
                c.get("no") if isinstance(c.get("no"), int) else 0,
                str(c.get("name") or ""),
            )
        )
        return chapters

    # ------------------------------------------------------------- 实体解析
    def resolve_entities(self, question: str, *, limit: int = 3) -> list[str]:
        """从问题文本中识别出现的人物实体名（子串匹配 name/别名）。"""
        q = str(question or "").strip()
        if not q:
            return []
        with self._get_driver().session() as session:
            rows = session.run(
                "MATCH (c:Character) RETURN c.name AS name, c.aliases AS aliases"
            ).data()
        found: list[str] = []
        for row in rows:
            name = str(row.get("name") or "")
            aliases = list(row.get("aliases") or [])
            if q == name or (name and name in q):
                found.append(name)
                continue
            if any(alias and alias in q for alias in aliases):
                found.append(name)
            if len(found) >= limit:
                break
        return found

    # ------------------------------------------------------------- Cypher
    def run_cypher(self, cypher: str) -> dict[str, Any]:
        """执行经过只读校验的 Cypher，返回行数据。"""
        _assert_read_only(cypher)
        with self._get_driver().session() as session:
            result = session.run(cypher)
            columns = list(result.keys())
            rows = []
            for record in result:
                rows.append({key: _to_primitive(record[key]) for key in columns})
                if len(rows) >= self._max_rows:
                    break
        return {"columns": columns, "rows": rows, "count": len(rows)}

    def _neighborhood(self, entities: list[str]) -> list[dict[str, Any]]:
        """邻域查询兜底：返回实体周围 depth 跳内的关系证据。"""
        if not entities:
            return []
        with self._get_driver().session() as session:
            rows = session.run(
                """
                MATCH (a)-[r]-(b)
                WHERE a.name IN $names
                RETURN a.name AS source, type(r) AS relation, b.name AS target,
                       b.brief AS target_brief
                LIMIT $limit
                """,
                names=entities,
                limit=self._limit,
            ).data()
        return [
            {
                "source": row["source"],
                "relation": row["relation"],
                "target": row["target"],
                "target_brief": str(row.get("target_brief") or ""),
            }
            for row in rows
            if row.get("source") and row.get("target")
        ]

    # ------------------------------------------------------------- 问答
    def ask(self, question: str) -> dict[str, Any]:
        entities = self.resolve_entities(question)
        evidence: list[dict[str, Any]] = []
        cypher = ""
        used: str = ""

        # 1) Text2Cypher（有实体时）
        if entities:
            cypher_plan = _text2cypher(question, entities)
            if cypher_plan.get("cypher"):
                cypher = str(cypher_plan["cypher"])
                try:
                    result = self.run_cypher(cypher)
                    if result["rows"]:
                        evidence = _flatten_cypher_rows(result)
                        used = "cypher"
                except Exception:  # noqa: BLE001 - Cypher 失败回退邻域查询
                    used = "fallback"

        # 2) 兜底：邻域查询
        if not evidence:
            evidence = self._neighborhood(entities)
            used = "neighborhood" if evidence else ("none" if not entities else "empty")

        # 3) LLM 合成回答
        answer = _synthesize(question, entities, evidence)

        return {
            "answer": answer,
            "entities": entities,
            "evidence": evidence[: self._limit],
            "strategy": used,
            "cypher": cypher,
        }


# --------------------------------------------------------------------- 内部
def _to_primitive(value: Any) -> Any:
    from neo4j.graph import Node, Relationship

    if isinstance(value, Node):
        return dict(value)
    if isinstance(value, Relationship):
        return {"type": value.type, **dict(value)}
    if isinstance(value, list):
        return [_to_primitive(item) for item in value]
    if isinstance(value, dict):
        return {k: _to_primitive(v) for k, v in value.items()}
    return value


def _flatten_cypher_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """把 Cypher 行结果转成可读的 relation 证据（对齐邻域查询结构）。"""
    out: list[dict[str, Any]] = []
    for row in result["rows"]:
        # 兼容 MATCH (a)-[r]->(b) RETURN a.name AS a, type(r) AS r, b.name AS b
        # 也兼容直接 RETURN name 等单值结果
        if {"a", "r", "b"} <= set(row) or {"source", "relation", "target"} <= set(row):
            out.append(
                {
                    "source": str(row.get("a") or row.get("source") or ""),
                    "relation": str(row.get("r") or row.get("relation") or ""),
                    "target": str(row.get("b") or row.get("target") or ""),
                }
            )
        else:
            out.append({k: str(v) for k, v in row.items()})
    return out


def _text2cypher(question: str, entities: list[str]) -> dict[str, Any]:
    system = (
        "你是《红楼梦》知识图谱的 Cypher 生成器。图谱使用以下节点标签与关系：\n"
        "节点: Character(人物, 属性 name/aliases/gender/house/identity/brief/fate), "
        "Family(家族), Place(地点), Object(物品), Event(事件)。\n"
        "关系: FATHER_OF(父->子) MOTHER_OF(母->子) SPOUSE_OF SIBLING_OF COUSIN_OF "
        "AUNT_OF GRANDMOTHER_OF LOVES SERVES FRIEND_OF BELONGS_TO LIVES_AT OWNS "
        "APPEARS_IN LOCATED_IN。\n"
        '要求: 只返回 JSON {"cypher": "..."}。只生成只读 MATCH/WITH/RETURN 查询，'
        "禁止写语句。人物名称用参数或直接字面量（如 '贾宝玉'）。若无法构造查询，"
        '返回 {"cypher": ""}。'
    )
    user = (
        f"问题：{question}\n已识别实体：{json.dumps(entities, ensure_ascii=False)}\n"
        "请生成一条 Cypher 查询回答该问题。"
    )
    try:
        data = require_chat_json(system, user)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    return {"cypher": str(data.get("cypher") or "")}


def _synthesize(
    question: str,
    entities: list[str],
    evidence: list[dict[str, Any]],
) -> str:
    if not evidence:
        return (
            "抱歉，我在知识图谱中没有找到与该问题相关的可靠证据。"
            "你可以换个问法，例如询问“贾宝玉和贾政是什么关系”“林黛玉住在哪里”"
            "或“抄检大观园有哪些人参与”。"
        )
    lines = []
    for item in evidence[:40]:
        src = item.get("source") or ""
        rel = item.get("relation") or ""
        dst = item.get("target") or ""
        if src and rel and dst:
            label = _REL_LABEL.get(rel, rel)
            lines.append(f"- {src} --{label}({rel})--> {dst}")
        else:
            lines.append(f"- {json.dumps(item, ensure_ascii=False)}")
    evidence_text = "\n".join(lines)
    system = (
        "你是《红楼梦》知识图谱问答助手。请基于给定的图谱证据，用简洁、准确、"
        "口语化的中文回答用户问题。只依据证据作答；证据不足时如实说明。"
        "可以补充一句出处式的提示（例如‘据图谱关系’），但不要编造图谱外事实。"
    )
    user = (
        f"问题：{question}\n已识别人物：{json.dumps(entities, ensure_ascii=False)}\n"
        f"图谱证据：\n{evidence_text}\n\n请给出回答。"
    )
    try:
        return require_chat(system, user).strip()
    except Exception:  # noqa: BLE001 - LLM 不可用时给出证据摘要
        return "\n".join(lines)


def build_hlm_client(config: dict[str, Any]) -> HlmKgClient | None:
    """从租户配置构造客户端（未配置 Neo4j 时返回 None）。"""
    cfg = config or {}
    uri = str(cfg.get("neo4j_uri") or "").strip()
    user = str(cfg.get("neo4j_user") or "neo4j").strip()
    password = str(cfg.get("neo4j_password") or "").strip()
    if not uri or not password:
        return None
    return HlmKgClient(
        uri=uri,
        user=user,
        password=password,
        max_rows=int(cfg.get("max_rows") or _MAX_ROWS),
        neighborhood_depth=int(cfg.get("neighborhood_depth") or 2),
        neighborhood_limit=int(cfg.get("neighborhood_limit") or 40),
    )


__all__ = ["HlmKgClient", "build_hlm_client"]
