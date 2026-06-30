"""Registries for agent profiles, skills, and tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import AgentProfile, SkillDefinition, ToolDefinition


@dataclass
class AgentRegistry:
    _items: dict[str, AgentProfile] = field(default_factory=dict)

    def register(self, profile: AgentProfile) -> None:
        self._items[profile.name] = profile

    def get(self, name: str) -> AgentProfile:
        return self._items[name]

    def all(self) -> list[AgentProfile]:
        return list(self._items.values())


@dataclass
class SkillRegistry:
    _items: dict[str, SkillDefinition] = field(default_factory=dict)

    def register(self, skill: SkillDefinition) -> None:
        self._items[skill.name] = skill

    def get(self, name: str) -> SkillDefinition:
        return self._items[name]

    def has(self, name: str) -> bool:
        return name in self._items

    def all(self) -> list[SkillDefinition]:
        return list(self._items.values())


@dataclass
class ToolRegistry:
    _items: dict[str, ToolDefinition] = field(default_factory=dict)

    def register(self, tool: ToolDefinition) -> None:
        self._items[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        return self._items[name]

    def has(self, name: str) -> bool:
        return name in self._items

    def all(self) -> list[ToolDefinition]:
        return list(self._items.values())

    def subset(self, names: list[str]) -> dict[str, ToolDefinition]:
        return {name: self._items[name] for name in names}
