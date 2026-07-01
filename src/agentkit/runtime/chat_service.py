"""Conversational memory service for all chat-first agents.

Wraps the memory stack (``ConversationManager`` + store + retriever + extractor)
for the web/CLI layers. One ``ConversationStore`` and embedding provider are
shared; a ``ConversationManager`` is built (and cached) per agent so each agent
can have its own memory window / budget / retrieval settings.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agentkit.core.memory.context_builder import ContextBuilder
from agentkit.core.memory.embeddings import build_embedding_provider
from agentkit.core.memory.extractor import MemoryExtractor
from agentkit.core.memory.manager import ConversationManager
from agentkit.core.memory.retrieval import MemoryRetriever
from agentkit.core.memory.store import build_conversation_store
from agentkit.core.memory.summarizer import Summarizer
from agentkit.core.memory.tokenizer import HeuristicTokenEstimator
from agentkit.core.memory.vector_store import build_vector_store
from agentkit.core.prompt_library import PromptLibrary


def agent_actions_enabled(tenant_config: dict, agent_name: str) -> bool:
    """Whether this chat agent may execute governed skills/tools.

    ``actions_enabled`` is the only source of truth for action capability.
    Missing values are treated as ``False`` so new agents start answer-only
    until explicitly granted tool/process access.
    """
    for item in tenant_config.get("chat_agents", []):
        if isinstance(item, dict) and str(item.get("name")) == agent_name:
            return bool(item.get("actions_enabled", False))
    return False


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
        knowledge_service: Any = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._tenant_config = tenant_config
        self._agents = agents
        self._audit = audit
        self._settings = settings
        self._chat_fn = chat_fn
        self._store = build_conversation_store(settings, db_path)
        self._tokenizer = HeuristicTokenEstimator()
        self._prompts = PromptLibrary.from_tenant_config(tenant_config)
        self._embeddings = embedding_provider or build_embedding_provider(settings)
        self._knowledge = knowledge_service or self._build_knowledge_service()
        self._chat_agents = {
            str(item.get("name")): item
            for item in tenant_config.get("chat_agents", [])
            if isinstance(item, dict)
        }
        self._managers: dict[str, ConversationManager] = {}

    # -- public API -----------------------------------------------------------

    def is_chat_agent(self, agent_name: str) -> bool:
        return str(agent_name) in self._chat_agents

    def is_action_agent(self, agent_name: str) -> bool:
        return agent_actions_enabled(self._tenant_config, agent_name)

    def is_answer_agent(self, agent_name: str) -> bool:
        return self.is_chat_agent(agent_name) and not self.is_action_agent(agent_name)

    def chat(
        self,
        *,
        agent: str,
        user_id: str,
        message: str,
        conversation_id: str | None = None,
        roles: Sequence[str] = (),
    ) -> dict[str, Any]:
        manager = self._manager_for(agent)
        knowledge = self._retrieve_knowledge(
            agent=agent,
            user_id=user_id,
            message=message,
            roles=roles,
        )
        reply = manager.chat(
            tenant_id=self._tenant_id,
            agent=agent,
            user_id=user_id,
            text=message,
            conversation_id=conversation_id,
            persona=self._persona(agent),
            tool_catalog=self._tool_catalog(agent),
            retrieved_knowledge=knowledge,
        )
        return {
            "interaction_mode": "chat",
            "agent_kind": "answer",
            "assistant_text": reply.reply,
            "conversation_id": reply.conversation_id,
            "run_id": reply.run_id,
            "summary_updated": reply.summary_updated,
            "retrieved_knowledge": len(knowledge),
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

    def prepare_action_turn(
        self,
        *,
        agent: str,
        user_id: str,
        message: str,
        conversation_id: str | None = None,
        roles: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Prepare bounded memory context for an action-capable chat turn.

        This does not call the LLM or write messages. The governed graph still
        owns safety, routing, planning, approval, execution, and output review.
        """
        conversation_id = self._resolve_conversation_id(
            agent=agent,
            user_id=user_id,
            message=message,
            conversation_id=conversation_id,
        )
        manager = self._manager_for(agent)
        cfg = self._memory_config(agent)
        recent = self._store.recent_messages(
            conversation_id=conversation_id,
            limit=int(cfg["window_turns"]) * 2,
        )
        summary_row = self._store.get_summary(conversation_id)
        summary = str(summary_row["summary_text"]) if summary_row else ""
        memories = manager.retrieve_memories(
            tenant_id=self._tenant_id,
            agent=agent,
            user_id=user_id,
            query=message,
        )
        knowledge = self._retrieve_knowledge(
            agent=agent,
            user_id=user_id,
            message=message,
            roles=roles,
        )
        return {
            "conversation_id": conversation_id,
            "memory": {
                "summary": summary,
                "recent_messages": [
                    {"role": row["role"], "content": row["content"]}
                    for row in recent
                    if row.get("content")
                ],
                "retrieved_memories": memories,
                "retrieved_knowledge": knowledge,
            },
        }

    def record_action_turn(
        self,
        *,
        agent: str,
        user_id: str,
        conversation_id: str,
        user_message: str | None,
        assistant_text: str,
        run_id: str | None = None,
        extract_memories: bool = True,
    ) -> None:
        """Persist an action-capable graph turn into the same conversation store."""
        manager = self._manager_for(agent)
        manager.record_external_turn(
            tenant_id=self._tenant_id,
            agent=agent,
            user_id=user_id,
            conversation_id=conversation_id,
            user_text=user_message,
            assistant_text=assistant_text,
            run_id=run_id,
            extract_memories=extract_memories,
        )

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
            knowledge_cap_tokens=int(getattr(self._settings, "rag_context_cap_tokens", 1000)),
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

    def _build_knowledge_service(self) -> Any:
        if not bool(getattr(self._settings, "rag_enabled", False)):
            return None
        from agentkit.core.rag.service import build_knowledge_service

        return build_knowledge_service(
            self._settings,
            tenant_id=self._tenant_id,
            embeddings=self._embeddings,
        )

    def _retrieve_knowledge(
        self,
        *,
        agent: str,
        user_id: str,
        message: str,
        roles: Sequence[str],
    ) -> list[str]:
        if self._knowledge is None or not self._rag_enabled_for_agent(agent):
            return []
        try:
            return self._knowledge.retrieve_context(
                message,
                user_id=user_id,
                agent=agent,
                roles=tuple(str(role) for role in roles),
                k=int(getattr(self._settings, "rag_top_k", 5)),
            )
        except Exception:
            return []

    def _rag_enabled_for_agent(self, agent_name: str) -> bool:
        if not bool(getattr(self._settings, "rag_enabled", False)):
            return False
        agent_cfg = self._chat_agents.get(agent_name, {})
        rag_cfg = agent_cfg.get("rag", {}) if isinstance(agent_cfg, dict) else {}
        if isinstance(rag_cfg, dict) and "enabled" in rag_cfg:
            return bool(rag_cfg["enabled"])
        return True

    def _resolve_conversation_id(
        self,
        *,
        agent: str,
        user_id: str,
        message: str,
        conversation_id: str | None,
    ) -> str:
        if conversation_id is None:
            return self._store.create_conversation(
                tenant_id=self._tenant_id,
                agent=agent,
                user_id=user_id,
                title=message[:60] if message else "New conversation",
            )
        conv = self._store.get_conversation(conversation_id)
        if conv is None:
            raise ValueError(f"Unknown conversation_id: {conversation_id}")
        if (
            conv["tenant_id"] != self._tenant_id
            or conv["agent"] != agent
            or conv["user_id"] != user_id
        ):
            raise ValueError("Conversation does not belong to this user/agent.")
        return conversation_id

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
