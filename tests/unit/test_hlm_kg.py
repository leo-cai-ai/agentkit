"""《红楼梦》知识图谱问答：Cypher 证据规范化与列名约定测试。

回归背景：聊天入口走 ask() -> Text2Cypher，LLM 生成的 Cypher 常带自定义列名
（如 person1/rel_type/person2），旧 _flatten_cypher_rows 只认 a/r/b 或
source/relation/target，导致 evidence 退化成无结构 JSON 行，合成回答读不出
关系类型（“具体关系类型在证据中没有明确说明”），与图谱页面固定结构查询的
高质量回答不一致。
"""

from __future__ import annotations

from typing import Any

import pytest

from agentkit.core.knowledge import graph as kg


# --------------------------------------------------------------------- 容错规范化
class TestFlattenCypherRows:
    def test_conventional_a_r_b_columns(self) -> None:
        rows = [
            {
                "a": "贾宝玉",
                "r": "SPOUSE_OF",
                "b": "薛宝钗",
            }
        ]
        assert kg._flatten_cypher_rows({"rows": rows}) == [
            {"source": "贾宝玉", "relation": "SPOUSE_OF", "target": "薛宝钗"}
        ]

    def test_conventional_source_relation_target_columns(self) -> None:
        rows = [{"source": "贾宝玉", "relation": "COUSIN_OF", "target": "薛宝钗"}]
        assert kg._flatten_cypher_rows({"rows": rows}) == [
            {"source": "贾宝玉", "relation": "COUSIN_OF", "target": "薛宝钗"}
        ]

    def test_custom_person_columns(self) -> None:
        """LLM 常见自定义列名：person1/rel_type/person2。"""
        rows = [
            {
                "person1": "贾宝玉",
                "rel_type": "SPOUSE_OF",
                "person2": "薛宝钗",
            },
            {
                "person1": "贾宝玉",
                "rel_type": "LOVES",
                "person2": "林黛玉",
            },
        ]
        assert kg._flatten_cypher_rows({"rows": rows}) == [
            {"source": "贾宝玉", "relation": "SPOUSE_OF", "target": "薛宝钗"},
            {"source": "贾宝玉", "relation": "LOVES", "target": "林黛玉"},
        ]

    def test_custom_name_relationship_columns(self) -> None:
        """另一常见形态：name/relationship（单侧实体 + 关系）。"""
        rows = [{"name": "贾宝玉", "relationship": "SPOUSE_OF", "name_2": "薛宝钗"}]
        assert kg._flatten_cypher_rows({"rows": rows}) == [
            {"source": "贾宝玉", "relation": "SPOUSE_OF", "target": "薛宝钗"}
        ]

    def test_relationship_value_is_primitive_dict(self) -> None:
        """neo4j Relationship 经 _to_primitive 后是 {type: ...} dict。"""
        rows = [{"a": "贾宝玉", "r": {"type": "COUSIN_OF"}, "b": "薛宝钗"}]
        assert kg._flatten_cypher_rows({"rows": rows}) == [
            {"source": "贾宝玉", "relation": "COUSIN_OF", "target": "薛宝钗"}
        ]

    def test_relationship_value_bracketed(self) -> None:
        rows = [{"person1": "贾宝玉", "relationship": "[SPOUSE_OF]", "person2": "薛宝钗"}]
        assert kg._flatten_cypher_rows({"rows": rows}) == [
            {"source": "贾宝玉", "relation": "SPOUSE_OF", "target": "薛宝钗"}
        ]

    def test_unstructured_row_kept_as_is(self) -> None:
        """聚合结果（无实体/关系列）保持原样，由合成层自行处理。"""
        rows = [{"count": 3}]
        assert kg._flatten_cypher_rows({"rows": rows}) == [{"count": "3"}]

    def test_empty_rows(self) -> None:
        assert kg._flatten_cypher_rows({"rows": []}) == []


# --------------------------------------------------------------------- 列名约定
class TestText2CypherColumnConvention:
    def test_prompt_forces_conventional_columns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}

        def fake_json(system: str, user: str) -> dict[str, Any]:
            captured["system"] = system
            return {"cypher": "MATCH (a)-[r]->(b) RETURN a.name AS a, type(r) AS r, b.name AS b"}

        monkeypatch.setattr(kg, "require_chat_json", fake_json)
        plan = kg._text2cypher("贾宝玉和薛宝钗是什么关系", ["贾宝玉", "薛宝钗"])
        assert plan["cypher"].startswith("MATCH")
        assert "RETURN a.name AS a, type(r) AS r, b.name AS b" in captured["system"]
        assert "不得自定义列名" in captured["system"]
