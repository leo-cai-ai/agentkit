"""红楼梦知识图谱 Skill 的工具适配与租户级客户端工厂。"""

from __future__ import annotations

from typing import Any

from agentkit.core.knowledge.graph import HlmKgClient, build_hlm_client

MAX_QUERY_LENGTH = 300
MAX_CYPHER_LENGTH = 2000


def _require_client(
    client: HlmKgClient | None,
) -> HlmKgClient:
    if client is None:
        raise RuntimeError(
            "红楼梦知识图谱未配置：请检查租户配置 hongloumeng.neo4j_uri / "
            "neo4j_user / neo4j_password，并确认 Neo4j 已启动。"
        )
    return client


def ask_tool(args: dict[str, Any], client: HlmKgClient | None = None) -> dict[str, Any]:
    selected = _require_client(client)
    question = str(args.get("question") or "").strip()
    if not question:
        raise ValueError("红楼梦问答需要提供 question")
    if len(question) > MAX_QUERY_LENGTH:
        raise ValueError(f"问题过长，最多 {MAX_QUERY_LENGTH} 字")
    return selected.ask(question)


def query_tool(args: dict[str, Any], client: HlmKgClient | None = None) -> dict[str, Any]:
    selected = _require_client(client)
    cypher = str(args.get("cypher") or "").strip()
    if not cypher:
        raise ValueError("hlm.graph.query 需要提供 cypher")
    if len(cypher) > MAX_CYPHER_LENGTH:
        raise ValueError(f"Cypher 过长，最多 {MAX_CYPHER_LENGTH} 字符")
    return selected.run_cypher(cypher)


def search_tool(args: dict[str, Any], client: HlmKgClient | None = None) -> dict[str, Any]:
    selected = _require_client(client)
    query = str(args.get("query") or "").strip()
    return {"entities": selected.search_entities(query)}


def build_handlers(tenant_config: dict[str, Any]) -> dict[str, Any]:
    """为一个租户构造共享 Neo4j 客户端的完整工具 handler 集合。

    客户端懒创建：Neo4j 未配置或不可用时，运行时仍可启动，调用工具时给出明确报错。
    """
    configured = tenant_config.get("hongloumeng", {})
    if not isinstance(configured, dict):
        configured = {}
    client = build_hlm_client(configured)
    return {
        "hlm.kg.ask": lambda args: ask_tool(args, client),
        "hlm.graph.query": lambda args: query_tool(args, client),
        "hlm.entity.search": lambda args: search_tool(args, client),
    }


__all__ = ["ask_tool", "query_tool", "search_tool", "build_handlers"]
