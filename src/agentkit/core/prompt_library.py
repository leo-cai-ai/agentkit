"""Overridable system-prompt library for LLM nodes.

Each LLM node owns an in-code default system prompt (the stable contract layer).
Tenants may override any node prompt by providing a ``nodes.<key>`` entry in
their loaded prompts, and may prepend an agent persona (``agents.<name>``) as a
preamble. When nothing is configured, ``system()`` returns the in-code default
verbatim, so default behavior is unchanged.
"""

from __future__ import annotations

_NODE_PREFIX = "nodes."
_AGENT_PREFIX = "agents."


class PromptLibrary:
    def __init__(
        self,
        *,
        overrides: dict[str, str] | None = None,
        personas: dict[str, str] | None = None,
    ) -> None:
        self._overrides = dict(overrides or {})
        self._personas = dict(personas or {})

    @classmethod
    def from_tenant_config(cls, tenant_config: dict) -> PromptLibrary:
        prompts = tenant_config.get("prompts", {}) or {}
        overrides = {
            key[len(_NODE_PREFIX) :]: value
            for key, value in prompts.items()
            if key.startswith(_NODE_PREFIX)
        }
        personas = {
            key[len(_AGENT_PREFIX) :]: value
            for key, value in prompts.items()
            if key.startswith(_AGENT_PREFIX)
        }
        return cls(overrides=overrides, personas=personas)

    def system(self, key: str, default: str, *, persona: str | None = None) -> str:
        base = self._overrides.get(key, default)
        preamble = self.persona(persona)
        return f"{preamble}\n\n{base}" if preamble else base

    def persona(self, name: str | None) -> str:
        if not name:
            return ""
        return self._personas.get(name, "").strip()
