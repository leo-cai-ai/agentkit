from pathlib import Path


def test_production_code_has_no_legacy_prompt_runtime() -> None:
    assert not Path("src/agentkit/core/prompt_library.py").exists()
    assert not Path("src/agentkit/core/prompts.py").exists()
    assert not Path("src/agentkit/core/input_resolution.py").exists()
    assert not Path("src/agentkit/core/governance.py").exists()


def test_no_production_node_calls_require_chat_directly() -> None:
    # core/knowledge 的 graph/ingest 是独立于 Agent 运行时节点的叶子工具
    # （CLI kg-ingest 与 Web 图谱 API 直连，不经过 Context 装配管线），
    # 与 llm_client 一样属于基础设施层，直接使用 require_chat*。
    allowed = {
        Path("src/agentkit/core/llm_client.py"),
        Path("src/agentkit/core/context/invocation.py"),
        Path("src/agentkit/core/knowledge/graph.py"),
        Path("src/agentkit/core/knowledge/ingest.py"),
    }
    offenders = []
    for root in (Path("src/agentkit"), Path("skills")):
        for path in root.rglob("*.py"):
            if path in allowed or "eval" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if any(
                marker in text
                for marker in (
                    "require_chat(",
                    "require_chat_json(",
                    "require_chat_streaming(",
                )
            ):
                offenders.append(path.as_posix())
    assert offenders == []
