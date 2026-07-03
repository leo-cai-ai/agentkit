from pathlib import Path

import pytest

from agentkit.runtime.declarative_catalog import load_catalog
from tests.unit.test_declarative_catalog import _write_catalog


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"agent_changes": {"unexpected": True}}, "Extra inputs are not permitted"),
        (
            {"capability_changes": {"autonomy": {"max_model_calls": 20}}},
            "Skill 自主预算不能超过 Agent",
        ),
        (
            {
                "capability_changes": {
                    "execution": {
                        "reasoning": "react",
                        "orchestration": "single",
                        "tool_policy": "side_effect",
                    }
                }
            },
            "ReAct 不能声明 side_effect",
        ),
        ({"agent_changes": {"skills": ["missing.skill"]}}, "引用了未知 capability"),
        (
            {"capability_changes": {"tools": ["missing.tool"]}},
            "引用了未知工具",
        ),
        ({"tool_changes": {"server": None}}, "MCP Tool 必须声明 server 和 tool"),
    ],
)
def test_catalog_rejects_invalid_policy(
    tmp_path: Path, kwargs: dict, message: str
) -> None:
    _write_catalog(tmp_path, **kwargs)

    with pytest.raises(ValueError, match=message):
        load_catalog(tmp_path)


def test_enabled_agents_reject_unknown_agent(tmp_path: Path) -> None:
    from agentkit.runtime.declarative_catalog import resolve_enabled_agent_ids

    _write_catalog(tmp_path)
    catalog = load_catalog(tmp_path)

    with pytest.raises(ValueError, match="未知 Agent"):
        resolve_enabled_agent_ids(catalog, {"enabled_agents": ["missing"]})


def test_enabled_agents_must_be_explicit(tmp_path: Path) -> None:
    from agentkit.runtime.declarative_catalog import resolve_enabled_agent_ids

    _write_catalog(tmp_path)
    catalog = load_catalog(tmp_path)

    with pytest.raises(ValueError, match="enabled_agents"):
        resolve_enabled_agent_ids(catalog, {})
