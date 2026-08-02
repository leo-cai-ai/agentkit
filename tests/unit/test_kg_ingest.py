from __future__ import annotations

from agentkit.core.knowledge.ingest import _chapter_name, _normalize

SCHEMA = {
    "node_labels": ["Person", "Place", "Object", "Event", "Chapter"],
    "rel_types": ["APPEARS_IN", "IN_CHAPTER", "FRIEND_OF"],
}


def test_normalize_extracts_entities_relationships_and_chapter():
    data = {
        "entities": [
            {"name": "贾宝玉", "label": "Person", "aliases": ["宝玉"], "brief": "主角"},
            {"name": "林黛玉", "label": "Person", "aliases": [], "brief": "女主角"},
        ],
        "relationships": [{"source": "贾宝玉", "relation": "FRIEND_OF", "target": "林黛玉"}],
        "chapter": {
            "no": 23,
            "title": "西厢记妙词通戏语 牡丹亭艳曲警芳心",
            "summary": "共读西厢，情愫暗生。",
            "participants": ["贾宝玉", "林黛玉", "不存在的人"],
        },
    }
    entities, relationships, chapter = _normalize(data, SCHEMA, 40)

    assert set(entities) == {"Person::贾宝玉", "Person::林黛玉"}
    assert relationships == [("贾宝玉", "FRIEND_OF", "林黛玉")]
    assert chapter["no"] == 23
    assert "共读西厢" in chapter["summary"]
    # 参与者只保留本块真实出现的实体
    assert chapter["participants"] == ["贾宝玉", "林黛玉"]


def test_normalize_without_chapter_returns_none():
    data = {"entities": [], "relationships": []}
    entities, relationships, chapter = _normalize(data, SCHEMA, 40)

    assert chapter is None
    assert entities == {}
    assert relationships == []


def test_normalize_filters_chapter_entity_label():
    # Chapter 是结构节点，即使 LLM 误当作实体返回也不应进入实体集合
    data = {
        "entities": [{"name": "第二十三回", "label": "Chapter", "brief": ""}],
        "relationships": [],
    }
    entities, _, _ = _normalize(data, SCHEMA, 40)
    assert entities == {}


def test_normalize_rejects_unknown_labels_and_rels():
    data = {
        "entities": [{"name": "张三", "label": "Unknown", "brief": ""}],
        "relationships": [{"source": "贾宝玉", "relation": "HACKS", "target": "林黛玉"}],
    }
    entities, relationships, _ = _normalize(data, SCHEMA, 40)
    assert entities == {}
    assert relationships == []


def test_chapter_name_priority():
    assert _chapter_name({"no": 3, "title": "回目标题"}) == "回目标题"
    assert _chapter_name({"no": 3, "title": ""}) == "第3回"
    assert _chapter_name({"no": None, "title": ""}) == ""
