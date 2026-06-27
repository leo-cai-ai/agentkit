"""Conversational-agent service for ``mode: "chat"`` agents.

Wraps the memory stack (``ConversationManager`` + store + retriever + extractor)
for the web/CLI layers. One ``ConversationStore`` and embedding provider are
shared; a ``ConversationManager`` is built (and cached) per agent so each agent
can have its own memory window / budget / retrieval settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentkit.core.memory.context_builder import ContextBuilder
from agentkit.core.memory.embeddings import build_embedding_provider
from agentkit.core.memory.extractor import MemoryExtractor
from agentkit.core.memory.manager import ConversationManager
from agentkit.core.memory.retrieval import MemoryRetriever
from agentkit.core.memory.store import ConversationStore
from agentkit.core.memory.summarizer import Summarizer
from agentkit.core.memory.tokenizer import HeuristicTokenEstimator
from agentkit.core.memory.vector_store import build_vector_store
from agentkit.core.prompt_library import PromptLibrary

_VALID_MODES = {"command", "chat"}


def agent_mode(tenant_config: dict, agent_name: str) -> str:
    """Return the configured interaction mode for an agent (default ``command``)."""
    for item in tenant_config.get("chat_agents", []):
        if isinstance(item, dict) and str(item.get("name")) == agent_name:
            mode = str(item.get("mode") or "command").lower()
            return mode if mode in _VALID_MODES else "command"
    return "command"


class ChatService:
    def __init__(
        self,
        *,
        tenant_id: str,
        tenant_config: dict,
        db_path: str | Path,
        agents: Any,
        audit: Any,
        settings: Any,
        chat_fn: Any = None,
        embedding_provider: Any = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._tenant_config = tenant_config
        self._agents = agents
        self._audit = audit
        self._settings = settings
        self._chat_fn = chat_fn
        self._store = ConversationStore(db_path)
        self._tokenizer = HeuristicTokenEstimator()
        self._prompts = PromptLibrary.from_tenant_config(tenant_config)
        self._embeddings = embedding_provider or build_embedding_provider(settings)
        self._chat_agents = {
            str(item.get("name")): item
            for item in tenant_config.get("chat_agents", [])
            if isinstance(item, dict)
        }
        self._managers: dict[str, ConversationManager] = {}

    # -- public API -----------------------------------------------------------

    def is_chat_agent(self, agent_name: str) -> bool:
        return agent_mode(self._tenant_config, agent_name) == "chat"

    def chat(
        self,
        *,
        agent: str,
        user_id: str,
        message: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        manager = self._manager_for(agent)
        reply = manager.chat(
            tenant_id=self._tenant_id,
            agent=agent,
            user_id=user_id,
            text=message,
            conversation_id=conversation_id,
            persona=self._persona(agent),
            tool_catalog=self._tool_catalog(agent),
        )
        return {
            "assistant_text": reply.reply,
            "conversation_id": reply.conversation_id,
            "run_id": reply.run_id,
            "summary_updated": reply.summary_updated,
        }

    def list_conversations(self, *, agent: str, user_id: str) -> list[dict[str, Any]]:
        return self._store.list_conversations(
            tenant_id=self._tenant_id, agent=agent, user_id=user_id
        )

    def new_conversation(self, *, agent: str, user_id: str, title: str | None = None) -> str:
        return self._store.create_conversation(
            tenant_id=self._tenant_id, agent=agent, user_id=user_id, title=title
        )

    def messages(self, *, conversation_id: str, user_id: str) -> list[dict[str, Any]]:
        conv = self._store.get_conversation(conversation_id)
        # Scope check: never expose another user's conversation.
        if conv is None or conv["tenant_id"] != self._tenant_id or conv["user_id"] != user_id:
            return []
        return [
            {"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
            for m in self._store.all_messages(conversation_id)
        ]

    # -- internals ------------------------------------------------------------

    def _memory_config(self, agent_name: str) -> dict[str, Any]:
        s = self._settings
        config = {
            "window_turns": int(getattr(s, "memory_window_turns", 6)),
            "max_context_tokens": int(getattr(s, "memory_max_context_tokens", 4000)),
            "summary_cap_tokens": int(getattr(s, "memory_summary_cap_tokens", 600)),
            "retrieval_k": int(getattr(s, "memory_retrieval_k", 4)),
            "extract_every_n_turns": int(getattr(s, "memory_extract_every_n_turns", 3)),
            "extract_memories": True,
        }
        override = self._chat_agents.get(agent_name, {}).get("memory", {}) or {}
        for key in config:
            if key in override:
                config[key] = override[key]
        return config

    def _manager_for(self, agent_name: str) -> ConversationManager:
        cached = self._managers.get(agent_name)
        if cached is not None:
            return cached
        cfg = self._memory_config(agent_name)
        builder = ContextBuilder(
            tokenizer=self._tokenizer,
            budget_tokens=int(cfg["max_context_tokens"]),
            window_turns=int(cfg["window_turns"]),
            summary_cap_tokens=int(cfg["summary_cap_tokens"]),
        )
        retriever = MemoryRetriever(
            vector_store=build_vector_store(self._settings, self._store),
            embeddings=self._embeddings,
            min_score=float(getattr(self._settings, "memory_min_retrieval_score", 0.1)),
            dedup_threshold=float(getattr(self._settings, "memory_dedup_threshold", 0.92)),
        )
        extractor = MemoryExtractor(chat_fn=self._chat_fn) if cfg["extract_memories"] else None
        manager = ConversationManager(
            store=self._store,
            builder=builder,
            summarizer=Summarizer(chat_fn=self._chat_fn),
            tokenizer=self._tokenizer,
            chat_fn=self._chat_fn,
            audit=self._audit,
            retriever=retriever,
            extractor=extractor,
            retrieval_k=int(cfg["retrieval_k"]),
            extract_every_n_turns=int(cfg["extract_every_n_turns"]),
        )
        self._managers[agent_name] = manager
        return manager

    def _persona(self, agent_name: str) -> str:
        try:
            profile = self._agents.get(agent_name)
        except KeyError:
            return ""
        personas = self._tenant_config.get("domain_personas", {})
        persona_key = personas.get(profile.domain) or agent_name
        persona = self._prompts.persona(persona_key)
        return persona or profile.description

    def _tool_catalog(self, agent_name: str) -> str:
        try:
            profile = self._agents.get(agent_name)
        except KeyError:
            return ""
        allowed = set(profile.allowed_skills)
        if not allowed:
            return ""
        lines = [
            f"- {skill['name']}: {skill.get('description', '')}".rstrip()
            for skill in self._tenant_config.get("skill_catalog", [])
            if skill.get("name") in allowed
        ]
        return "\n".join(lines)
