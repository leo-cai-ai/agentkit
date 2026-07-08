"""General Agent 协调、显式提及和业务 Agent 能力目录。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentkit.runtime.conversation_context import ConversationContextService
from agentkit.runtime.conversation_persistence import ConversationPersistenceService
from agentkit.runtime.conversation_projection import ConversationProjectionService
from agentkit.runtime.conversation_projection_models import (
    AcceptedTurn,
    AttemptStage,
    AttemptStatus,
)

from .audit import TERMINAL_RUN_STATUSES
from .context.models import ContextRenderRequest
from .contracts import TaskRequest, TaskResponse
from .memory.store import ConversationConflictError
from .registry import AgentRegistry
from .response_text import format_task_output_text

GENERAL_AGENT_ID = "general_agent"


class AgentMentionError(ValueError):
    """当前消息中的 Agent 提及无效。"""


class UnknownAgentMentionError(AgentMentionError):
    """消息提及了当前租户不可用的 Agent。"""


class MultipleAgentMentionsError(AgentMentionError):
    """一条消息显式提及了多个 Agent。"""


@dataclass(frozen=True)
class MentionResult:
    agent_id: str | None
    task_text: str
    mention: str = ""


class AgentDirectory:
    """提供租户已启用 Agent 的显示信息和安全能力卡。"""

    def __init__(
        self,
        *,
        agents: AgentRegistry,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self._agents = {profile.name: profile for profile in agents.all()}
        self._config = dict(config or {})
        unknown = sorted(set(self._config) - set(self._agents))
        if unknown:
            raise ValueError(f"Agent 目录引用了未启用 Agent: {', '.join(unknown)}")
        self._aliases = self._build_aliases()

    def business_cards(self) -> list[dict[str, Any]]:
        """返回适合放入 General 路由上下文的最小能力描述。"""
        cards: list[dict[str, Any]] = []
        for agent_id, profile in sorted(self._agents.items()):
            if agent_id == GENERAL_AGENT_ID:
                continue
            entry = self._entry(agent_id)
            cards.append(
                {
                    "id": agent_id,
                    "label": entry["label"],
                    "aliases": list(entry["aliases"]),
                    "domain": profile.domain,
                    "description": profile.description,
                    "skills": list(profile.allowed_skills),
                    "routing_keywords": list(profile.routing_keywords),
                }
            )
        return cards

    def all_cards(self) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        business = {item["id"]: item for item in self.business_cards()}
        for agent_id in sorted(self._agents):
            if agent_id in business:
                cards.append(business[agent_id])
                continue
            profile = self._agents[agent_id]
            entry = self._entry(agent_id)
            cards.append(
                {
                    "id": agent_id,
                    "label": entry["label"],
                    "aliases": list(entry["aliases"]),
                    "domain": profile.domain,
                    "description": profile.description,
                    "skills": list(profile.allowed_skills),
                    "routing_keywords": list(profile.routing_keywords),
                }
            )
        return cards

    def resolve(self, value: str) -> str | None:
        return self._aliases.get(value.strip().casefold())

    def profile(self, agent_id: str):
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise UnknownAgentMentionError(f"未知 Agent: {agent_id}") from exc

    def mention_names(self) -> list[str]:
        return sorted(self._aliases, key=lambda item: (-len(item), item))

    def _entry(self, agent_id: str) -> dict[str, Any]:
        raw = self._config.get(agent_id)
        value = dict(raw) if isinstance(raw, Mapping) else {}
        label = str(value.get("label") or agent_id)
        aliases = [str(item) for item in value.get("aliases", []) if str(item).strip()]
        return {"label": label, "aliases": aliases}

    def _build_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for agent_id in sorted(self._agents):
            entry = self._entry(agent_id)
            names = [agent_id, entry["label"], *entry["aliases"]]
            for name in names:
                normalized = str(name).strip().casefold()
                if not normalized:
                    continue
                owner = aliases.get(normalized)
                if owner is not None and owner != agent_id:
                    raise ValueError(f"Agent 提及别名冲突: {name}")
                aliases[normalized] = agent_id
        return aliases


class AgentMentionParser:
    """只解析当前消息，不保存或继承任何 Agent 选择状态。"""

    _unknown_pattern = re.compile(r"(?:^|[\s，,。！？!?])@([^\s，,。！？!?]+)")

    def __init__(self, directory: AgentDirectory) -> None:
        self._directory = directory
        names = directory.mention_names()
        alternatives = "|".join(re.escape(item) for item in names)
        self._known_pattern = re.compile(
            rf"(?P<prefix>^|[\s，,。！？!?])@(?P<name>{alternatives})(?![\w.-])",
            flags=re.IGNORECASE,
        )

    def parse(self, text: str) -> MentionResult:
        message = str(text or "").strip()
        matches = list(self._known_pattern.finditer(message))
        unknown = [
            match.group(1)
            for match in self._unknown_pattern.finditer(message)
            if self._directory.resolve(match.group(1)) is None
        ]
        if unknown:
            raise UnknownAgentMentionError(f"未知 Agent: @{unknown[0]}")
        agent_ids = {self._directory.resolve(match.group("name")) for match in matches}
        agent_ids.discard(None)
        if len(agent_ids) > 1:
            raise MultipleAgentMentionsError("一条消息只能显式指定一个 Agent")
        if not matches:
            return MentionResult(agent_id=None, task_text=message)

        agent_id = next(iter(agent_ids))
        pieces: list[str] = []
        cursor = 0
        for match in matches:
            pieces.append(message[cursor : match.start()])
            pieces.append(match.group("prefix"))
            cursor = match.end()
        pieces.append(message[cursor:])
        task_text = re.sub(r"\s+", " ", "".join(pieces)).strip(" ，,。")
        return MentionResult(
            agent_id=agent_id,
            task_text=task_text,
            mention=matches[0].group("name"),
        )


class MultiAgentCoordinator:
    """以 General Agent 为会话所有者的多 Agent 聊天协调器。"""

    def __init__(
        self,
        *,
        tenant_id: str,
        tenant_selector: str,
        directory: AgentDirectory,
        gateway: Any,
        audit: Any,
        context_invoker: Any,
        conversation_context: ConversationContextService,
        conversation_persistence: ConversationPersistenceService,
        conversation_projection: ConversationProjectionService,
    ) -> None:
        self._tenant_id = tenant_id
        self._tenant_selector = tenant_selector
        self._directory = directory
        self._mentions = AgentMentionParser(directory)
        self._gateway = gateway
        self._audit = audit
        self._context_invoker = context_invoker
        self._conversation_context = conversation_context
        self._conversation_persistence = conversation_persistence
        self._projection = conversation_projection

    def handle(self, request: TaskRequest) -> TaskResponse:
        conversation_id = str(request.context["conversation_id"])
        turn_id = str(request.context["conversation_turn_id"])
        attempt_id = str(request.context["conversation_attempt_id"])
        accepted = self._projection.resolve_accepted(
            conversation_id=conversation_id,
            turn_id=turn_id,
            attempt_id=attempt_id,
        )
        parent_run_id = self._audit.start_run(
            tenant_id=self._tenant_id,
            user_id=request.user_id,
            text=request.text,
            agent_id=GENERAL_AGENT_ID,
            conversation_id=conversation_id,
        )
        retry_of_run_id = str(request.context.get("retry_of_run_id") or "")
        if retry_of_run_id:
            self._audit.record(
                parent_run_id,
                "conversation_retry_started",
                {"retry_of_run_id": retry_of_run_id},
            )
        attempt_bound = False
        try:
            self._projection.bind_run(
                attempt_id,
                run_id=parent_run_id,
                agent_id=GENERAL_AGENT_ID,
            )
            attempt_bound = True
            self._projection.set_stage(attempt_id, AttemptStage.UNDERSTANDING_REQUEST)
            return self._handle_started(
                request=request,
                accepted=accepted,
                parent_run_id=parent_run_id,
            )
        except Exception as exc:
            self._fail_parent_run(
                parent_run_id,
                attempt_id,
                exc,
                fail_attempt=attempt_bound,
            )
            raise

    def _handle_started(
        self,
        *,
        request: TaskRequest,
        accepted: AcceptedTurn,
        parent_run_id: str,
    ) -> TaskResponse:
        conversation_id = accepted.conversation_id
        from .safety import REFUSAL_MESSAGE, build_safety_guard

        safety = build_safety_guard().inspect_input(request.text)
        if safety.action == "block":
            self._audit.record(parent_run_id, "safety_blocked", safety.to_audit())
            return self._finish_general(
                request=request,
                accepted=accepted,
                parent_run_id=parent_run_id,
                status="blocked",
                message=REFUSAL_MESSAGE,
                route={"type": "safety_block", "reason": "输入安全策略阻止"},
            )

        general = self._directory.profile(GENERAL_AGENT_ID)
        general_context = self._conversation_context.build(
            agent=general,
            tenant_id=self._tenant_id,
            agent_id=GENERAL_AGENT_ID,
            user_id=request.user_id,
            conversation_id=conversation_id,
            run_id=parent_run_id,
            message=request.text,
            roles=request.roles,
            exclude_turn_id=accepted.turn_id,
        )
        self._projection.set_stage(accepted.attempt_id, AttemptStage.ROUTING_AGENT)
        try:
            mention = self._mentions.parse(request.text)
        except AgentMentionError as exc:
            return self._finish_general(
                request=request,
                accepted=accepted,
                parent_run_id=parent_run_id,
                status="needs_clarification",
                message=f"{exc}。当前可用：{self._available_agent_text()}",
                route={"type": "mention_error", "reason": str(exc)},
            )

        decision: dict[str, Any]
        if mention.agent_id and mention.agent_id != GENERAL_AGENT_ID:
            decision = {
                "action": "delegate",
                "target_agent": mention.agent_id,
                "task": mention.task_text or request.text,
                "reason": f"用户显式指定 @{mention.mention}",
                "confidence": "high",
            }
            route_type = "explicit_mention"
        elif mention.agent_id == GENERAL_AGENT_ID:
            decision = {
                "action": "answer",
                "target_agent": None,
                "task": mention.task_text,
                "reason": "用户显式指定 General Agent",
                "confidence": "high",
            }
            route_type = "explicit_mention"
        else:
            try:
                decision = self._route(
                    request=request,
                    run_id=parent_run_id,
                    general=general,
                    context=general_context,
                )
            except Exception as exc:  # 路由模型失败时禁止误委派，也禁止生成虚假执行进度
                self._audit.record(
                    parent_run_id,
                    "agent_route_failed",
                    {"error_type": type(exc).__name__, "reason": str(exc)},
                )
                route = {
                    "type": "route_failed",
                    "action": "clarify",
                    "target_agent": None,
                    "reason": "路由决策未通过结构校验，已停止执行",
                    "confidence": "low",
                }
                self._audit.record(parent_run_id, "agent_route_decided", route)
                return self._finish_general(
                    request=request,
                    accepted=accepted,
                    parent_run_id=parent_run_id,
                    status="needs_clarification",
                    message=(
                        "本轮路由决策未通过结构校验，因此未调用任何 Agent、Skill 或 Tool。"
                        f"请重试，或使用明确的 @Agent 指定本轮执行者。当前可用："
                        f"{self._available_agent_text()}"
                    ),
                    route=route,
                )
            route_type = (
                "general_delegate" if decision["action"] == "delegate" else "general_answer"
            )

        route = {
            "type": route_type,
            "action": decision["action"],
            "target_agent": decision.get("target_agent"),
            "reason": decision["reason"],
            "confidence": decision["confidence"],
        }
        self._audit.record(parent_run_id, "agent_route_decided", route)
        if decision["action"] == "delegate":
            return self._delegate(
                request=request,
                accepted=accepted,
                parent_run_id=parent_run_id,
                decision=decision,
                route=route,
            )

        self._projection.set_stage(accepted.attempt_id, AttemptStage.EXECUTING_AGENT)
        answer = self._answer(
            request=request,
            run_id=parent_run_id,
            general=general,
            context=general_context,
            decision=decision,
        )
        status = "needs_clarification" if decision["action"] == "clarify" else "completed"
        return self._finish_general(
            request=request,
            accepted=accepted,
            parent_run_id=parent_run_id,
            status=status,
            message=answer,
            route=route,
        )

    def resume(
        self,
        thread_id: str,
        *,
        user_id: str,
        roles: list[str],
        approved_skills: list[str] | tuple[str, ...] = (),
        rejected_skills: list[str] | tuple[str, ...] = (),
        decision_context: dict[str, Any] | None = None,
    ) -> TaskResponse:
        """恢复业务子运行，并把最终结果写回原 General 会话。"""
        child_run = self._audit.run_for_thread(
            thread_id,
            tenant_id=self._tenant_id,
            user_id=user_id,
        )
        if child_run is None:
            raise KeyError(f"未知或无权访问的审批线程: {thread_id}")
        parent_run_id = str(child_run.get("parent_run_id") or "")
        if not parent_run_id:
            raise RuntimeError("审批线程不属于 General Agent 委派运行")
        parent_run = self._audit.get_run(parent_run_id)
        if parent_run is None or parent_run.get("user_id") != user_id:
            raise RuntimeError("审批父运行不存在或不属于当前用户")
        conversation_id = str(child_run.get("conversation_id") or "")
        target_agent = str(child_run.get("agent_id") or "")
        self._directory.profile(target_agent)
        accepted, action_id = self._accepted_for_thread(
            conversation_id=conversation_id,
            user_id=user_id,
            thread_id=thread_id,
        )
        try:
            self._projection.set_stage(accepted.attempt_id, AttemptStage.EXECUTING_AGENT)
            return self._resume_started(
                thread_id=thread_id,
                user_id=user_id,
                roles=roles,
                approved_skills=approved_skills,
                rejected_skills=rejected_skills,
                decision_context=decision_context,
                child_run=child_run,
                parent_run=parent_run,
                parent_run_id=parent_run_id,
                conversation_id=conversation_id,
                target_agent=target_agent,
                accepted=accepted,
                action_id=action_id,
            )
        except Exception as exc:
            action_failed = False
            try:
                self._projection.fail_approval(
                    action_id,
                    error_code=type(exc).__name__,
                    error_summary="审批恢复流程异常退出",
                )
                action_failed = True
            except Exception:  # noqa: BLE001 - 清理失败不得遮蔽原始业务异常
                pass
            self._fail_parent_run(
                parent_run_id,
                accepted.attempt_id,
                exc,
                fail_attempt=not action_failed,
            )
            raise

    def _resume_started(
        self,
        *,
        thread_id: str,
        user_id: str,
        roles: list[str],
        approved_skills: list[str] | tuple[str, ...],
        rejected_skills: list[str] | tuple[str, ...],
        decision_context: dict[str, Any] | None,
        child_run: dict[str, Any],
        parent_run: dict[str, Any],
        parent_run_id: str,
        conversation_id: str,
        target_agent: str,
        accepted: AcceptedTurn,
        action_id: str,
    ) -> TaskResponse:
        child = self._gateway.resume(
            thread_id,
            approved_skills=approved_skills,
            rejected_skills=rejected_skills,
            decision_context=decision_context,
        )
        self._audit.record(
            parent_run_id,
            "run_resumed",
            {"child_run_id": child_run["run_id"], "thread_id": thread_id},
        )
        route = self._route_event(parent_run_id)
        delegation = {
            "parent_run_id": parent_run_id,
            "child_run_id": child.run_id,
            "source_agent": GENERAL_AGENT_ID,
            "target_agent": target_agent,
            "status": child.status,
        }
        if child.status == "waiting_for_approval":
            self._projection.set_stage(
                accepted.attempt_id,
                AttemptStage.PREPARING_APPROVAL,
            )
            approval = child.output.get("approval", {})
            if not isinstance(approval, dict):
                approval = {}
            governance_approval = child.governance.get("approval", {})
            if not isinstance(governance_approval, dict):
                governance_approval = {}
            approval = {**governance_approval, **approval}
            preview = approval.get("preview", {})
            preview = dict(preview) if isinstance(preview, dict) else {}
            preview.setdefault(
                "content",
                format_task_output_text(status=child.status, output=child.output),
            )
            skills = approval.get("skills", [])
            consumed_decision = "approved" if approved_skills or not rejected_skills else "rejected"
            consumed_context = dict(decision_context or {})
            consumed_context.setdefault("approved_skills", [str(item) for item in approved_skills])
            consumed_context.setdefault("rejected_skills", [str(item) for item in rejected_skills])
            self._projection.rollover_approval(
                accepted=accepted,
                current_action_id=action_id,
                run_id=parent_run_id,
                decision=consumed_decision,
                decided_by=user_id,
                decision_context=consumed_context,
                agent_id=target_agent,
                thread_id=child.thread_id,
                skills=[str(item) for item in skills] if isinstance(skills, list) else [],
                preview=preview,
                preview_artifact_id=(
                    str(approval["preview_artifact_id"])
                    if approval.get("preview_artifact_id")
                    else None
                ),
            )
            self._audit.record(
                parent_run_id,
                "run_paused",
                {"status": "waiting_for_approval", "child_run_id": child.run_id},
            )
        else:
            self._project_terminal(
                accepted=accepted,
                user_id=user_id,
                run_id=parent_run_id,
                assistant_agent_id=target_agent,
                status=child.status,
                output=child.output,
                approval_action_id=action_id,
            )
            self._audit.record(
                parent_run_id,
                "run_finished",
                {"status": child.status},
            )
        return TaskResponse(
            status=child.status,
            output=child.output,
            run_id=parent_run_id,
            thread_id=child.thread_id,
            agent=target_agent,
            strategy=child.strategy,
            conversation_id=conversation_id,
            governance={
                **child.governance,
                "route": route,
                "delegation": delegation,
            },
            audit_events=self._audit.events_for(parent_run_id),
        )

    def _route(self, *, request, run_id, general, context) -> dict[str, Any]:
        result = self._context_invoker.invoke_json(
            ContextRenderRequest(
                context_id="runtime.agent-route",
                tenant_id=self._tenant_id,
                tenant_selector=self._tenant_selector,
                run_id=run_id,
                agent=general,
                skill=None,
                values={
                    "request.message": request.text,
                    "conversation.summary": context.summary,
                    "conversation.recent_messages": list(context.recent_messages),
                    "routing.candidate_agents": self._directory.business_cards(),
                },
                global_token_limit=general.max_tokens,
            )
        )
        value = dict(result.value)
        action = str(value.get("action") or "")
        if action not in {"answer", "clarify", "delegate"}:
            raise ValueError(f"General Agent 返回了无效动作: {action}")
        target = value.get("target_agent")
        business_ids = {card["id"] for card in self._directory.business_cards()}
        if action == "delegate" and target not in business_ids:
            raise ValueError(f"General Agent 路由到了未启用 Agent: {target}")
        if action != "delegate":
            value["target_agent"] = None
        value["task"] = str(value.get("task") or request.text).strip()
        value["reason"] = str(value.get("reason") or "需要进一步判断").strip()
        value["confidence"] = str(value.get("confidence") or "low")
        return value

    def _answer(self, *, request, run_id, general, context, decision) -> str:
        result = self._context_invoker.invoke_streaming(
            ContextRenderRequest(
                context_id="runtime.general-answer",
                tenant_id=self._tenant_id,
                tenant_selector=self._tenant_selector,
                run_id=run_id,
                agent=general,
                skill=None,
                values={
                    "request.message": request.text,
                    "conversation.summary": context.summary,
                    "conversation.recent_messages": list(context.recent_messages),
                    "routing.candidate_agents": self._directory.business_cards(),
                    "routing.decision": decision,
                },
                global_token_limit=general.max_tokens,
            )
        )
        return str(result.value)

    def _delegate(
        self,
        *,
        request: TaskRequest,
        accepted: AcceptedTurn,
        parent_run_id: str,
        decision: dict[str, Any],
        route: dict[str, Any],
    ) -> TaskResponse:
        conversation_id = accepted.conversation_id
        self._projection.set_stage(accepted.attempt_id, AttemptStage.EXECUTING_AGENT)
        target_agent = str(decision["target_agent"])
        target = self._directory.profile(target_agent)
        task_text = str(decision.get("task") or request.text).strip()
        delegated_context = self._conversation_context.build_for_delegation(
            agent=target,
            tenant_id=self._tenant_id,
            owner_agent_id=GENERAL_AGENT_ID,
            user_id=request.user_id,
            conversation_id=conversation_id,
            run_id=parent_run_id,
            message=task_text,
            roles=request.roles,
            exclude_turn_id=accepted.turn_id,
        )
        child_request = TaskRequest(
            user_id=request.user_id,
            roles=list(request.roles),
            text=task_text,
            context={
                **{
                    key: value for key, value in request.context.items() if key != "conversation_id"
                },
                "agent": target_agent,
                "parent_run_id": parent_run_id,
                "trace_conversation_id": conversation_id,
                "original_user_message": request.text,
                "agent_context": {
                    "summary": delegated_context.summary,
                    "recent_messages": list(delegated_context.recent_messages),
                    "memories": list(delegated_context.memories),
                    "knowledge": list(delegated_context.knowledge),
                },
            },
        )
        child = self._gateway.handle_delegated(child_request)
        delegation = {
            "parent_run_id": parent_run_id,
            "child_run_id": child.run_id,
            "source_agent": GENERAL_AGENT_ID,
            "target_agent": target_agent,
            "status": child.status,
        }
        self._audit.record(parent_run_id, "agent_delegated", delegation)
        if child.status == "waiting_for_approval":
            self._projection.set_stage(
                accepted.attempt_id,
                AttemptStage.PREPARING_APPROVAL,
            )
            approval = child.output.get("approval", {})
            if not isinstance(approval, dict):
                approval = {}
            governance_approval = child.governance.get("approval", {})
            if not isinstance(governance_approval, dict):
                governance_approval = {}
            approval = {**governance_approval, **approval}
            preview = approval.get("preview", {})
            preview = dict(preview) if isinstance(preview, dict) else {}
            preview.setdefault(
                "content",
                format_task_output_text(status=child.status, output=child.output),
            )
            skills = approval.get("skills", [])
            self._projection.request_approval(
                accepted=accepted,
                run_id=parent_run_id,
                agent_id=target_agent,
                thread_id=child.thread_id,
                skills=[str(item) for item in skills] if isinstance(skills, list) else [],
                preview=preview,
                preview_artifact_id=(
                    str(approval["preview_artifact_id"])
                    if approval.get("preview_artifact_id")
                    else None
                ),
            )
            self._audit.record(
                parent_run_id,
                "run_paused",
                {"status": "waiting_for_approval", "child_run_id": child.run_id},
            )
        else:
            self._project_terminal(
                accepted=accepted,
                user_id=request.user_id,
                run_id=parent_run_id,
                assistant_agent_id=target_agent,
                status=child.status,
                output=child.output,
            )
            self._audit.record(parent_run_id, "run_finished", {"status": child.status})
        return TaskResponse(
            status=child.status,
            output=child.output,
            run_id=parent_run_id,
            thread_id=child.thread_id,
            agent=target_agent,
            strategy=child.strategy,
            conversation_id=conversation_id,
            governance={
                **child.governance,
                "route": route,
                "delegation": delegation,
            },
            audit_events=self._audit.events_for(parent_run_id),
        )

    def _finish_general(
        self,
        *,
        request: TaskRequest,
        accepted: AcceptedTurn,
        parent_run_id: str,
        status: str,
        message: str,
        route: dict[str, Any],
    ) -> TaskResponse:
        output = {"message": message}
        self._project_terminal(
            accepted=accepted,
            user_id=request.user_id,
            run_id=parent_run_id,
            assistant_agent_id=GENERAL_AGENT_ID,
            status=status,
            output=output,
        )
        self._audit.record(parent_run_id, "run_finished", {"status": status})
        return TaskResponse(
            status=status,
            output=output,
            run_id=parent_run_id,
            thread_id="",
            agent=GENERAL_AGENT_ID,
            strategy="direct",
            conversation_id=accepted.conversation_id,
            governance={"route": route},
            audit_events=self._audit.events_for(parent_run_id),
        )

    def _project_terminal(
        self,
        *,
        accepted: AcceptedTurn,
        user_id: str,
        run_id: str,
        assistant_agent_id: str,
        status: str,
        output: dict[str, Any],
        approval_action_id: str | None = None,
    ) -> None:
        terminal_status = self._attempt_status(status)
        self._projection.set_stage(accepted.attempt_id, AttemptStage.FINALIZING)
        content = format_task_output_text(status=status, output=output)
        if approval_action_id is not None:
            self._projection.project_approval_output(
                accepted=accepted,
                action_id=approval_action_id,
                run_id=run_id,
                agent_id=assistant_agent_id,
                content=content,
                status=terminal_status,
            )
        else:
            self._projection.project_output(
                accepted=accepted,
                run_id=run_id,
                agent_id=assistant_agent_id,
                content=content,
                status=terminal_status,
            )
        if terminal_status is not AttemptStatus.SUCCEEDED:
            return
        general = self._directory.profile(GENERAL_AGENT_ID)
        self._conversation_persistence.finalize_canonical_turn(
            tenant_id=self._tenant_id,
            agent_id=GENERAL_AGENT_ID,
            user_id=user_id,
            conversation_id=accepted.conversation_id,
            turn_id=accepted.turn_id,
            run_id=run_id,
            window_turns=general.context_policy.memory.window_turns,
        )

    def _fail_parent_run(
        self,
        run_id: str,
        attempt_id: str,
        exc: Exception,
        *,
        fail_attempt: bool = True,
    ) -> None:
        """异常退出时保证 General 父运行进入失败终态。"""
        try:
            current = self._audit.get_run(run_id)
        except Exception:  # noqa: BLE001 - 清理失败不得遮蔽原始业务异常
            return
        if current is None or current.get("status") in TERMINAL_RUN_STATUSES:
            return
        try:
            self._audit.record(
                run_id,
                "run_failed",
                {
                    "error_type": type(exc).__name__,
                    "reason": "General Agent 协调流程异常退出",
                },
            )
        except Exception:  # noqa: BLE001 - 清理失败不得遮蔽原始业务异常
            pass
        if fail_attempt:
            try:
                self._projection.fail_attempt(
                    attempt_id,
                    error_code=type(exc).__name__,
                    error_summary="General Agent 协调流程异常退出",
                )
            except Exception:  # noqa: BLE001 - 清理失败不得遮蔽原始业务异常
                pass
        try:
            self._audit.record(run_id, "run_finished", {"status": "failed"})
        except Exception:  # noqa: BLE001 - 清理失败不得遮蔽原始业务异常
            pass

    @staticmethod
    def _attempt_status(status: str) -> AttemptStatus:
        if status == "completed":
            return AttemptStatus.SUCCEEDED
        if status in {"blocked", "needs_clarification", "rejected"}:
            return AttemptStatus.REJECTED
        if status == "cancelled":
            return AttemptStatus.CANCELLED
        if status == "interrupted":
            return AttemptStatus.INTERRUPTED
        return AttemptStatus.FAILED

    def _available_agent_text(self) -> str:
        return "、".join(
            f"@{card['aliases'][0] if card['aliases'] else card['label']}"
            for card in self._directory.business_cards()
        )

    def _accepted_for_thread(
        self,
        *,
        conversation_id: str,
        user_id: str,
        thread_id: str,
    ) -> tuple[AcceptedTurn, str]:
        """从 durable Action 定位原 Attempt，恢复时绝不创建第二条用户消息。"""
        timeline = self._projection.timeline(
            conversation_id=conversation_id,
            tenant_id=self._tenant_id,
            user_id=user_id,
        )
        active: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for turn in timeline.turns:
            for attempt in turn["attempts"]:
                for action in attempt["actions"]:
                    if action.get("thread_id") == thread_id and action.get("status") in {
                        "pending",
                        "approved",
                        "rejected",
                    }:
                        active.append((turn, attempt, action))
        if not active:
            raise KeyError(f"审批线程 inactive: {thread_id}")
        if len(active) > 1:
            raise ConversationConflictError(
                f"approval thread has multiple active actions: {thread_id}"
            )
        turn, attempt, action = active[0]
        return (
            self._projection.resolve_accepted(
                conversation_id=conversation_id,
                turn_id=str(turn["id"]),
                attempt_id=str(attempt["id"]),
            ),
            str(action["id"]),
        )

    def _route_event(self, run_id: str) -> dict[str, Any]:
        for event in self._audit.events_for(run_id):
            if event.get("type") == "agent_route_decided":
                return dict(event.get("payload") or {})
        return {"type": "general_delegate", "target_agent": None, "reason": ""}

    def _retry_origin(self, run_id: str) -> str:
        """从父 Run 审计中恢复跨审批暂停的可信重试关联。"""
        for event in reversed(self._audit.events_for(run_id)):
            if event.get("type") == "conversation_retry_started":
                payload = dict(event.get("payload") or {})
                return str(payload.get("retry_of_run_id") or "")
        return ""


__all__ = [
    "AgentDirectory",
    "AgentMentionError",
    "AgentMentionParser",
    "MultiAgentCoordinator",
    "GENERAL_AGENT_ID",
    "MentionResult",
    "MultipleAgentMentionsError",
    "UnknownAgentMentionError",
]
