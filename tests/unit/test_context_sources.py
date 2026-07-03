from __future__ import annotations

import pytest

from agentkit.core.context.sources import ContextSourceRegistry


def test_source_registry_rejects_unknown_source() -> None:
    registry = ContextSourceRegistry.default()

    with pytest.raises(ValueError, match="未注册 Context Source"):
        registry.require_source("request.raw_context")


def test_canonical_json_is_deterministic() -> None:
    registry = ContextSourceRegistry.default()

    assert registry.serialize("canonical_json", {"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_highest_score_truncation_is_stable() -> None:
    registry = ContextSourceRegistry.default()
    values = [
        {"id": "b", "score": 1},
        {"id": "a", "score": 1},
        {"id": "c", "score": 2},
    ]

    assert registry.truncate_items("highest_score", values, 2) == [
        {"id": "c", "score": 2},
        {"id": "a", "score": 1},
    ]


def test_newest_truncation_keeps_original_order_of_tail() -> None:
    registry = ContextSourceRegistry.default()

    assert registry.truncate_items("newest", [1, 2, 3], 2) == [2, 3]


def test_registry_rejects_unknown_serializer_and_truncator() -> None:
    registry = ContextSourceRegistry.default()

    with pytest.raises(ValueError, match="Serializer"):
        registry.require_serializer("yaml")
    with pytest.raises(ValueError, match="Truncator"):
        registry.require_truncator("random")
