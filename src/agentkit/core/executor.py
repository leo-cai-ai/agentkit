"""Executor for skill plans, including batch sharding."""

from __future__ import annotations

import json
from dataclasses import asdict

from .artifacts import ArtifactRecord, InMemoryArtifactStore
from .audit import InMemoryAuditLog, PostgresAuditLog, SQLiteAuditLog
from .contracts import IntentFrame, SkillContext, TaskPlan, TaskRequest
from .conversation import ConversationFallback
from .llm_client import require_chat_json
from .policy import PolicyGuard
from .prompt_library import PromptLibrary
from .registry import SkillRegistry, ToolRegistry
from .schema_validation import SkillInputError, validate_skill_input, validate_skill_output
from .tool_executor import ToolExecutor

DEFAULT_EXECUTE_BRIEF_SYSTEM = (
    "You are the LLM execute-preflight node in a governed LangGraph runtime. "
    "Return only valid JSON with keys: execution_goal, expected_outputs, risks. "
    "Do not execute tools or invent results. Summarize what the deterministic executor "
    "is about to do from the approved plan."
)


class PlanExecutor:
    def __init__(
        self,
        *,
        tenant_id: str,
        tenant_config: dict,
        skills: SkillRegistry,
        tools: ToolRegistry,
        policy: PolicyGuard,
        audit: InMemoryAuditLog | SQLiteAuditLog | PostgresAuditLog,
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._tenant_config = tenant_config
        self._skills = skills
        self._tools = tools
        self._policy = policy
        self._audit = audit
        self._prompts = prompt_library or PromptLibrary()
        self._conversation = ConversationFallback(
            tenant_id=tenant_id,
            tenant_config=tenant_config,
            prompt_library=self._prompts,
        )

    def execute(
        self,
        *,
        run_id: str,
        request: TaskRequest,
        plan: TaskPlan,
        intent: IntentFrame,
    ) -> dict:
        execution_brief = self._llm_execution_brief(request=request, plan=plan, intent=intent)
        self._audit.record(run_id, "execution_llm_briefed", execution_brief)

        # One hardened tool invoker per run: timeout/retry/idempotency/audit, with
        # a run-scoped idempotency cache (so the same key isn't re-executed within
        # the run, and never reused across runs).
        invoker = self._build_tool_invoker(run_id)
        artifacts = InMemoryArtifactStore(
            on_write=lambda record: self._record_artifact(run_id, record)
        )

        if not plan.steps:
            self._audit.record(
                run_id,
                "conversation_fallback",
                {"reason": plan.route.reason, "intent_type": intent.intent_type},
            )
            response = self._conversation.respond(
                request,
                intent=intent,
                route_reason=plan.route.reason,
            )
            response["execution_brief"] = execution_brief
            return response

        step_outputs: list[dict] = []
        for step in plan.steps:
            skill = self._skills.get(step.skill_name)
            decision = self._policy.check_skill(request=request, skill=skill)
            self._audit.record(
                run_id,
                "policy_checked",
                {
                    "skill": skill.name,
                    "allowed": decision.allowed,
                    "reason": decision.reason,
                    "requires_approval": decision.requires_approval,
                },
            )
            if not decision.allowed:
                return {
                    "error": "policy_denied",
                    "reason": decision.reason,
                    "execution_brief": execution_brief,
                }
            if decision.requires_approval:
                return {
                    "status": "waiting_for_approval",
                    "skill": skill.name,
                    "execution_brief": execution_brief,
                }

            try:
                validate_skill_input(skill, step.args)
            except SkillInputError as exc:
                self._audit.record(
                    run_id,
                    "skill_input_invalid",
                    {"step_id": step.step_id, "skill": skill.name, "reason": str(exc)},
                )
                return {
                    "error": "input_validation_failed",
                    "reason": str(exc),
                    "skill": skill.name,
                    "execution_brief": execution_brief,
                }

            self._audit.record(
                run_id,
                "step_started",
                {"step_id": step.step_id, "skill": skill.name, "mode": step.mode},
            )

            ctx = SkillContext(
                tenant_id=self._tenant_id,
                tenant_config=self._tenant_config,
                tools=self._tools.subset(skill.tools),
                request=request,
                invoker=invoker,
                artifacts=artifacts,
            )
            if step.mode == "batch" and skill.batch_key:
                result = self._execute_batch(ctx=ctx, skill_name=skill.name, args=step.args)
            else:
                result = skill.handler(ctx, step.args)

            output_warnings = validate_skill_output(skill, result)
            if output_warnings:
                self._audit.record(
                    run_id,
                    "skill_output_invalid",
                    {"step_id": step.step_id, "skill": skill.name, "warnings": output_warnings},
                )
                if isinstance(result, dict):
                    result["_schema_warnings"] = output_warnings

            self._audit.record(
                run_id,
                "step_finished",
                {"step_id": step.step_id, "skill": skill.name, "mode": step.mode},
            )
            step_outputs.append(result)

        return {
            "steps": step_outputs,
            "final": step_outputs[-1] if step_outputs else {},
            "artifacts": [record.ref() for record in artifacts.list()],
            "execution_brief": execution_brief,
        }

    def _record_artifact(self, run_id: str, record: ArtifactRecord) -> None:
        self._audit.record(run_id, "artifact_written", record.ref())

    def _build_tool_invoker(self, run_id: str) -> ToolExecutor:
        timeout = 30.0
        max_workers = 32
        max_retries = 0
        retry_base_delay = 0.2
        try:
            from agentkit.config import get_settings

            settings = get_settings()
            timeout = float(getattr(settings, "tool_timeout_seconds", timeout))
            max_workers = int(getattr(settings, "tool_max_workers", max_workers))
            max_retries = int(getattr(settings, "tool_max_retries", max_retries))
            retry_base_delay = float(getattr(settings, "tool_retry_base_delay", retry_base_delay))
        except Exception:  # noqa: BLE001 - settings optional in lightweight tests
            pass
        return ToolExecutor(
            tenant_id=self._tenant_id,
            audit=self._audit,
            run_id=run_id,
            timeout_seconds=timeout,
            max_workers=max_workers,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
        )

    def _execute_batch(self, *, ctx: SkillContext, skill_name: str, args: dict) -> dict:
        skill = self._skills.get(skill_name)
        batch_key = skill.batch_key
        assert batch_key is not None

        values = list(args.get(batch_key, []))
        shard_size = int(self._tenant_config.get("batch_size", 50))
        shards = [values[i : i + shard_size] for i in range(0, len(values), shard_size)]

        shard_results: list[dict] = []
        for _index, shard in enumerate(shards, start=1):
            shard_args = dict(args)
            shard_args[batch_key] = shard
            # Hint so per-shard handlers can skip once-per-request work (e.g. an
            # LLM summary) and let the merge step produce it on the final result.
            shard_args["_batch_shard"] = True
            shard_results.append(skill.handler(ctx, shard_args))

        if hasattr(skill.handler, "merge_batch"):
            return skill.handler.merge_batch(shard_results, args)  # type: ignore[attr-defined]

        return {"_batched": True, "shard_count": len(shards), "results": shard_results}

    def _persona_for_plan(self, plan: TaskPlan) -> str | None:
        domain_personas = self._tenant_config.get("domain_personas", {})
        if not isinstance(domain_personas, dict) or not domain_personas:
            return None
        for step in plan.steps:
            if self._skills.has(step.skill_name):
                domain = self._skills.get(step.skill_name).domain
                persona = domain_personas.get(domain)
                if persona:
                    return str(persona)
        return None

    def _llm_execution_brief(
        self,
        *,
        request: TaskRequest,
        plan: TaskPlan,
        intent: IntentFrame,
    ) -> dict:
        data = require_chat_json(
            self._prompts.system(
                "execute_brief",
                DEFAULT_EXECUTE_BRIEF_SYSTEM,
                persona=self._persona_for_plan(plan),
            ),
            json.dumps(
                {
                    "message": request.text,
                    "roles": request.roles,
                    "context": request.context,
                    "intent": asdict(intent),
                    "plan": asdict(plan),
                },
                ensure_ascii=False,
                default=str,
            ),
        )
        expected_outputs = data.get("expected_outputs")
        risks = data.get("risks")
        return {
            "execution_goal": str(data.get("execution_goal") or intent.goal),
            "expected_outputs": [str(item) for item in expected_outputs]
            if isinstance(expected_outputs, list)
            else [],
            "risks": [str(item) for item in risks] if isinstance(risks, list) else [],
            "llm_required": True,
        }
