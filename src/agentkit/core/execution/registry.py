"""ExecutionStrategy 的显式注册表。"""

from __future__ import annotations

from collections.abc import Iterable

from .protocol import ExecutionStrategy
from .selector import StrategyPolicyError


class StrategyRegistry:
    def __init__(self, strategies: Iterable[ExecutionStrategy]) -> None:
        self._items = {strategy.name: strategy for strategy in strategies}

    def get(self, name: str) -> ExecutionStrategy:
        try:
            return self._items[name]
        except KeyError as exc:
            raise StrategyPolicyError(f"未注册执行策略: {name}") from exc


__all__ = ["StrategyRegistry"]
