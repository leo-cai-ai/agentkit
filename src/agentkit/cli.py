"""Console entry point for agentkit."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any

from agentkit.core.contracts import TaskRequest
from agentkit.core.logging_config import configure_logging
from agentkit.runtime.bootstrap import build_runtime


def _run_demo(tenant_id: str | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    configure_logging()
    runtime = build_runtime(tenant_id=tenant_id)
    request = TaskRequest(
        user_id="u-001",
        roles=["recruiter"],
        text="Rank the top 3 candidates for JOB-001 and explain why.",
        context={
            "agent": "hr_recruiter",
            "skill": "candidate.rank",
            "job_id": "JOB-001",
            "candidate_ids": ["C-100", "C-101", "C-102", "C-103", "C-104"],
            "top_n": 3,
        },
    )
    response = runtime.gateway.handle(request)
    print(json.dumps(response.to_dict(), ensure_ascii=False, indent=2))


def _run_web() -> None:
    configure_logging()
    from agentkit.web.app import app

    app.run(host="127.0.0.1", port=8501)


def _browser_login(
    site: str,
    *,
    query: str,
    target: str,
    tenant_id: str | None = None,
) -> int:
    """Open a persistent headed browser profile for a human-managed site login."""

    configure_logging()
    if site != "xhs":
        print(f"Unsupported browser site: {site}", file=sys.stderr)
        return 2

    from agentkit.runtime.bootstrap import AGENTKIT_ROOT, load_tenant_config, resolve_tenant_id
    from agentkit.runtime.declarative_catalog import load_catalog, load_tool_factory

    tenant_config = load_tenant_config(resolve_tenant_id(tenant_id))
    catalog = load_catalog(AGENTKIT_ROOT)
    factory = load_tool_factory(catalog, "xhs.rpa.search_top_notes")
    handlers = factory(tenant_config)
    interactive_login = handlers.get("__interactive_login__")
    if not callable(interactive_login):
        print("XHS Tool 工厂未提供交互式登录入口。", file=sys.stderr)
        return 2
    print(
        "已打开持久化小红书浏览器。请在窗口中手动完成扫码、短信或其他验证；"
        "在目标页完成认证前浏览器会保持打开，可按 Ctrl+C 取消。"
    )
    interactive_login({"target": target, "query": query})
    print("已检测到认证完成的目标页，浏览器会话已保存。")
    return 0


def _check_postgres(settings: Any) -> bool:
    """Verify PostgreSQL connectivity and required extensions."""
    from agentkit.core.pg import connection, require_psycopg

    target = (
        "(AGENTKIT_PG_DSN)"
        if settings.pg_dsn
        else (
            f"host={settings.pg_host} port={settings.pg_port} db={settings.pg_database} "
            f"user={settings.pg_user} sslmode={settings.pg_sslmode}"
        )
    )
    print(f"[..] postgres {target}")
    try:
        require_psycopg()
    except RuntimeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return False
    try:
        with connection(settings) as conn:
            version = conn.execute("SELECT version()").fetchone()[0]
            print(f"[ok] connected: {version}")
            if str(getattr(settings, "vector_store_backend", "sqlite")).lower() == "postgres":
                has_ext = conn.execute(
                    "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
                ).fetchone()
                if has_ext is None:
                    print("[..] pgvector extension missing; attempting CREATE EXTENSION")
                    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    print("[ok] pgvector extension created")
                else:
                    print("[ok] pgvector extension present")
    except Exception as exc:  # noqa: BLE001 - report any driver/connection error
        print(f"[FAIL] postgres connectivity error: {exc}", file=sys.stderr)
        return False
    return True


def _ensure_postgres_schemas(settings: Any) -> bool:
    """Ensure PostgreSQL storage schemas needed by the configured backends."""
    from agentkit.core.audit import PostgresAuditLog
    from agentkit.core.memory.pg_store import PgConversationStore
    from agentkit.core.memory.pg_vector_store import PgVectorStore

    try:
        storage_backend = str(getattr(settings, "storage_backend", "sqlite")).lower()
        vector_backend = str(getattr(settings, "vector_store_backend", "sqlite")).lower()
        if storage_backend == "postgres":
            PostgresAuditLog(settings)
            print("[ok] audit tables + indexes ready")
            PgConversationStore(settings)
            print("[ok] conversation tables + indexes ready")
        if vector_backend == "postgres":
            PgVectorStore(settings)._ensure_schema()
            print("[ok] pgvector memories table + index ready")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] could not ensure postgres schemas: {exc}", file=sys.stderr)
        return False
    return True


def _init_db() -> int:
    """Create/verify storage and check connectivity. Returns a process exit code."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    configure_logging()

    from agentkit.config import get_settings
    from agentkit.core.migrations import run_storage_migrations
    from agentkit.runtime.bootstrap import DATA_DIR, resolve_tenant_id

    settings = get_settings()
    ok = True

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"[ok] data dir writable: {DATA_DIR}")
    except OSError as exc:
        ok = False
        print(f"[FAIL] data dir not writable: {DATA_DIR}: {exc}", file=sys.stderr)

    storage_backend = settings.storage_backend
    vector_backend = settings.vector_store_backend
    print(f"[..] storage_backend = {storage_backend}")
    print(f"[..] vector_store_backend = {vector_backend}")
    uses_postgres = storage_backend == "postgres" or vector_backend == "postgres"
    postgres_ready = True
    if uses_postgres:
        postgres_ready = _check_postgres(settings)
        ok = postgres_ready and ok

    migrations_ready = False
    if postgres_ready:
        tenant_id = resolve_tenant_id()
        sqlite_path = DATA_DIR / f"{tenant_id}.sqlite"
        try:
            applied = run_storage_migrations(settings, sqlite_path=sqlite_path)
            print(f"[ok] runtime migrations ready: {applied or 'up-to-date'}")
            migrations_ready = True
        except Exception as exc:  # noqa: BLE001 - report migration failures to CLI users
            ok = False
            print(f"[FAIL] could not apply runtime migrations: {exc}", file=sys.stderr)

    if uses_postgres and postgres_ready and migrations_ready:
        ok = _ensure_postgres_schemas(settings) and ok
    elif not uses_postgres:
        print("[ok] sqlite runtime storage + vector store (no external database required)")

    if ok:
        print("\ninit-db OK")
        return 0
    print("\ninit-db FAILED", file=sys.stderr)
    return 1


def _new_tenant(tenant_id: str, *, force: bool) -> None:
    from agentkit.runtime.bootstrap import TENANTS_DIR
    from agentkit.runtime.scaffold import create_tenant

    path = create_tenant(tenant_id, root=TENANTS_DIR, force=force)
    print(f"已创建租户配置: {path}")


def _new_agent(agent_id: str) -> None:
    from agentkit.runtime.bootstrap import AGENTKIT_ROOT
    from agentkit.runtime.scaffold import create_agent

    path = create_agent(agent_id, root=AGENTKIT_ROOT / "agents")
    print(f"已创建 Agent Manifest: {path}")


def _new_skill(package_id: str) -> None:
    from agentkit.runtime.bootstrap import AGENTKIT_ROOT
    from agentkit.runtime.scaffold import create_skill

    path = create_skill(package_id, root=AGENTKIT_ROOT / "skills")
    print(f"已创建 Skill 包: {path}")


def _validate_catalog(*, as_json: bool) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    configure_logging()

    from agentkit.config import get_settings
    from agentkit.runtime.bootstrap import AGENTKIT_ROOT, build_global_budget
    from agentkit.runtime.declarative_catalog import load_catalog

    try:
        catalog = load_catalog(
            AGENTKIT_ROOT,
            global_budget=build_global_budget(get_settings()),
        )
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    result = {
        "agents": len(catalog.agents),
        "capabilities": len(catalog.capabilities),
        "tools": len(catalog.tools),
    }
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "[ok] 声明目录有效: "
            f"{result['agents']} Agents, {result['capabilities']} Capabilities, "
            f"{result['tools']} Tools"
        )
    return 0


def _runtime_doctor_checks(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """返回不调用 LLM 的部署预检结果。"""
    from agentkit.config import get_settings
    from agentkit.runtime.bootstrap import (
        AGENTKIT_ROOT,
        build_global_budget,
        load_tenant_config,
        resolve_tenant_id,
    )
    from agentkit.runtime.declarative_catalog import load_catalog, resolve_enabled_agent_ids

    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    resolved_tenant = resolve_tenant_id(tenant_id)
    try:
        tenant_config = load_tenant_config(resolved_tenant)
    except Exception as exc:  # noqa: BLE001 - doctor reports instead of traceback
        add("tenant config", False, str(exc))
        return checks
    add("tenant config", True, resolved_tenant)

    try:
        catalog = load_catalog(
            AGENTKIT_ROOT,
            global_budget=build_global_budget(get_settings()),
        )
        selected = resolve_enabled_agent_ids(catalog, tenant_config)
    except (OSError, ValueError) as exc:
        add("declarative catalog", False, str(exc))
        return checks
    add(
        "declarative catalog",
        True,
        f"{len(catalog.agents)} agents, {len(catalog.capabilities)} capabilities, "
        f"{len(catalog.tools)} tools",
    )
    add("enabled agents", bool(selected), ", ".join(sorted(selected)))

    try:
        from agentkit.core.context import ContextRegistry

        contexts = ContextRegistry(
            root=AGENTKIT_ROOT / "contexts",
            tenant_selector=resolved_tenant,
            overrides=dict(tenant_config.get("context_overrides") or {}),
            global_token_limit=get_settings().llm_context_window_tokens,
        )
    except Exception as exc:  # noqa: BLE001 - doctor 需要返回结构化失败项
        add("context registry", False, str(exc))
        return checks
    add(
        "context registry",
        True,
        f"{len(contexts.manifest())} packs, {contexts.manifest_hash}",
    )

    try:
        runtime = build_runtime(tenant_id=resolved_tenant)
    except Exception as exc:  # noqa: BLE001
        add("runtime build", False, str(exc))
        return checks
    add("runtime build", True, f"tenant_id={runtime.tenant_config.get('tenant_id')}")

    registered_agents = {agent.name for agent in runtime.gateway.agents.all()}
    registered_skills = {skill.name for skill in runtime.gateway.skills.all()}
    registered_tools = {tool.name for tool in runtime.gateway.tools.all()}

    role_permissions = tenant_config.get("role_permissions", {})
    if not isinstance(role_permissions, dict):
        add("role_permissions", False, "must be an object")
    else:
        malformed = [
            str(role)
            for role, permissions in role_permissions.items()
            if not isinstance(permissions, list)
            or any(not isinstance(permission, str) for permission in permissions)
        ]
        add(
            "role_permissions",
            not malformed,
            "malformed roles: " + ", ".join(sorted(malformed))
            if malformed
            else f"{len(role_permissions)} roles",
        )

    add("registered agents", bool(registered_agents), ", ".join(sorted(registered_agents)))
    add("registered skills", True, ", ".join(sorted(registered_skills)) or "(none)")
    add("registered tools", True, ", ".join(sorted(registered_tools)) or "(none)")
    return checks


def _validate_contexts(*, tenant_id: str | None, as_json: bool) -> int:
    """严格加载 Context Registry，不调用模型。"""
    try:
        from agentkit.config import get_settings
        from agentkit.core.context import ContextRegistry
        from agentkit.runtime.bootstrap import AGENTKIT_ROOT, load_tenant_config, resolve_tenant_id

        resolved = resolve_tenant_id(tenant_id)
        tenant_config = load_tenant_config(resolved)
        registry = ContextRegistry(
            root=AGENTKIT_ROOT / "contexts",
            tenant_selector=resolved,
            overrides=dict(tenant_config.get("context_overrides") or {}),
            global_token_limit=get_settings().llm_context_window_tokens,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 仅输出安全错误摘要
        print(f"[FAIL] Context Registry 无效: {exc}", file=sys.stderr)
        return 1
    packs = registry.manifest()
    result = {
        "count": len(packs),
        "manifest_hash": registry.manifest_hash,
        "packs": packs,
    }
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[ok] Context Registry: {result['count']} packs, {result['manifest_hash']}")
        for item in packs:
            print(
                f"  {item['id']} v{item['version']} {item['hash']} "
                f"budget={item['max_input_tokens']}"
            )
    return 0


def _doctor(*, tenant_id: str | None, skip_db: bool, as_json: bool) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    configure_logging()

    ok = True
    storage_code = None
    if not skip_db:
        storage_code = _init_db()
        ok = storage_code == 0

    checks = _runtime_doctor_checks(tenant_id)
    ok = all(bool(check["passed"]) for check in checks) and ok

    if as_json:
        print(
            json.dumps(
                {
                    "passed": ok,
                    "storage_exit_code": storage_code,
                    "checks": checks,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for check in checks:
            status = "ok" if check["passed"] else "FAIL"
            detail = f": {check['detail']}" if check.get("detail") else ""
            print(f"[{status}] {check['name']}{detail}")
        if ok:
            print("\ndoctor OK")
        else:
            print("\ndoctor FAILED", file=sys.stderr)
    return 0 if ok else 1


def _run_eval(
    dataset: str,
    *,
    target_kind: str,
    threshold: float,
    min_mean_score: float,
    use_judge: bool,
    as_json: bool,
    tenant_id: str | None,
) -> int:
    """Run a golden dataset and return a process exit code (0 pass, 1 regression)."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    configure_logging()

    from agentkit.eval import (
        LLMJudge,
        llm_target,
        load_cases,
        make_gateway_target,
        make_gateway_trace_target,
        run_eval,
    )

    cases = load_cases(dataset)
    if target_kind == "gateway":
        runtime = build_runtime(tenant_id=tenant_id)
        target = make_gateway_target(runtime)
    elif target_kind == "gateway-trace":
        runtime = build_runtime(tenant_id=tenant_id)
        target = make_gateway_trace_target(runtime)
    else:
        target = llm_target
    judge = LLMJudge() if use_judge else None

    report = run_eval(cases, target, judge=judge)
    if as_json:
        print(
            json.dumps(
                {"summary": report.summary(), "results": [r.to_dict() for r in report.results]},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(report.format_text())

    passed = report.gate(min_pass_rate=threshold, min_mean_score=min_mean_score)
    if not passed:
        print(
            f"\nREGRESSION GATE FAILED: pass_rate={report.pass_rate:.2%} "
            f"(min {threshold:.2%}), mean_score={report.mean_score:.2f} "
            f"(min {min_mean_score:.2f})",
            file=sys.stderr,
        )
    return 0 if passed else 1


def _run_eval_suite(
    suite_path: str,
    *,
    use_judge: bool,
    as_json: bool,
    tenant_id: str | None,
    output_path: str | None,
    baseline_path: str | None,
    validate_only: bool,
) -> int:
    """运行版本化评测套件，并持久化可比较的 JSON 报告。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    configure_logging()

    from datetime import UTC, datetime

    from agentkit.config import get_settings
    from agentkit.eval import (
        LLMJudge,
        llm_target,
        load_eval_suite,
        load_suite_cases,
        make_gateway_target,
        make_gateway_trace_target,
        resolve_dataset_paths,
        run_eval,
    )
    from agentkit.eval.report import build_run_report, write_run_report

    suite = load_eval_suite(suite_path)
    cases = load_suite_cases(suite, suite_path=suite_path)
    dataset_paths = resolve_dataset_paths(suite, suite_path=suite_path)
    if validate_only:
        payload = {
            "suite": suite.id,
            "version": suite.version,
            "target": suite.target,
            "cases": len(cases),
            "datasets": [str(path) for path in dataset_paths],
            "valid": True,
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Eval suite valid: {suite.id} ({len(cases)} cases)")
        return 0

    runtime = None
    if suite.target == "gateway":
        runtime = build_runtime(tenant_id=tenant_id)
        target = make_gateway_target(runtime)
    elif suite.target == "gateway-trace":
        runtime = build_runtime(tenant_id=tenant_id)
        target = make_gateway_trace_target(runtime)
    else:
        target = llm_target

    judge = LLMJudge() if use_judge else None
    report = run_eval(
        cases,
        target,
        judge=judge,
        require_judge=suite.gates.require_judge,
        repetitions=suite.execution.repetitions,
        concurrency=suite.execution.concurrency,
    )
    settings = get_settings()
    context_manifest_hash = ""
    if runtime is not None:
        context_manifest_hash = str(runtime.context_invoker.manifest_hash)
    run_report = build_run_report(
        report,
        suite_id=suite.id,
        suite_version=suite.version,
        target=suite.target,
        min_pass_rate=suite.gates.min_pass_rate,
        min_mean_score=suite.gates.min_mean_score,
        dataset_paths=dataset_paths,
        tenant_id=tenant_id or "",
        provider=settings.llm_provider,
        model=settings.openai_model or settings.llm_provider,
        context_manifest_hash=context_manifest_hash,
        repetitions=suite.execution.repetitions,
        concurrency=suite.execution.concurrency,
        baseline_path=baseline_path,
    )
    if output_path is None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_path = str(Path("evaluation") / "reports" / f"{suite.id}-{timestamp}.json")
    write_run_report(run_report, output_path)
    if as_json:
        print(json.dumps(run_report, ensure_ascii=False, indent=2))
    else:
        print(report.format_text())
        print(f"\nReport: {output_path}")
    return 0 if bool(run_report["gate"]["passed"]) else 1


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _ocr_check(image: str, *, as_json: bool) -> int:
    """使用生产 OCR Provider 对一张本地图片执行真实验收。"""

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from agentkit.config import get_settings
    from agentkit.core.ocr import OcrProviderError
    from agentkit.runtime.ocr import build_configured_ocr_provider

    try:
        settings = get_settings()
        provider = build_configured_ocr_provider(settings)
    except (OSError, ValueError) as exc:
        return _print_ocr_check_error(type(exc).__name__, as_json=as_json)
    if not provider.enabled:
        payload = {
            "status": "skipped",
            "text": "",
            "provider": provider.name,
            "model": provider.model,
            "reason": "ocr_not_configured",
            "usage": {},
        }
        if as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("SKIPPED: OCR provider is none")
        return 0

    image_path = Path(image)
    if not image_path.is_file():
        return _print_ocr_check_error("image_not_found", as_json=as_json)
    mime_type = (mimetypes.guess_type(image_path.name)[0] or "").lower()
    if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
        return _print_ocr_check_error("unsupported_mime_type", as_json=as_json)
    try:
        image_bytes = image_path.read_bytes()
        started = time.perf_counter()
        result = provider.analyze(
            image_bytes,
            mime_type=mime_type,
            hint=image_path.name,
        )
        elapsed_seconds = round(time.perf_counter() - started, 3)
    except OcrProviderError as exc:
        return _print_ocr_check_error(exc.code, as_json=as_json)
    except OSError:
        return _print_ocr_check_error("image_read_failed", as_json=as_json)

    result_payload: dict[str, Any] = {
        **result.to_dict(),
        "elapsed_seconds": elapsed_seconds,
    }
    if as_json:
        print(json.dumps(result_payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"OCR completed: provider={result.provider}, model={result.model}, "
            f"elapsed={elapsed_seconds:.3f}s"
        )
        if result.usage:
            print("usage=" + json.dumps(dict(result.usage), ensure_ascii=False))
        print(result.text)
    return 0


def _print_ocr_check_error(code: str, *, as_json: bool) -> int:
    payload = {"status": "failed", "reason": code}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    else:
        print(f"OCR check failed: {code}", file=sys.stderr)
    return 1


def _rag_service_for_tenant(tenant_selector: str | None):
    from agentkit.config import get_settings
    from agentkit.core.rag.service import build_knowledge_service
    from agentkit.runtime.bootstrap import build_runtime, load_tenant_config, resolve_tenant_id
    from agentkit.runtime.ocr import build_configured_ocr_provider

    resolved = resolve_tenant_id(tenant_selector)
    tenant_config = load_tenant_config(resolved)
    tenant_id = str(tenant_config.get("tenant_id") or resolved)
    runtime = build_runtime(tenant_id=resolved)
    settings = get_settings()
    service = build_knowledge_service(
        settings,
        tenant_id=tenant_id,
        tenant_selector=resolved,
        context_invoker=runtime.context_invoker,
        ocr_provider=build_configured_ocr_provider(settings),
    )
    return tenant_id, service, runtime.gateway.audit


def _rag_ingest(
    path: str,
    *,
    tenant_id: str | None,
    roles: str,
    ocr: bool | None,
    as_json: bool,
) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    configure_logging()
    from agentkit.config import get_settings

    logical_tenant_id, service, _audit = _rag_service_for_tenant(tenant_id)
    settings = get_settings()
    report = service.ingest_path(
        path,
        acl_roles=_parse_csv(roles),
        metadata={"tenant_id": logical_tenant_id},
        ocr_enabled=bool(getattr(settings, "rag_ocr_enabled", False) if ocr is None else ocr),
    )
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"[ok] RAG ingested {report['documents']} documents, "
            f"{report['chunks']} chunks for tenant {logical_tenant_id}"
        )
        for warning in report["warnings"]:
            print(f"  warning: {warning}")
        for skipped in report["skipped"]:
            print(f"  skipped: {skipped}")
    return 0


def _rag_query(
    text: str,
    *,
    tenant_id: str | None,
    agent: str,
    user_id: str,
    roles: str,
    k: int | None,
    as_json: bool,
) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    configure_logging()
    from agentkit.config import get_settings

    logical_tenant_id, service, audit = _rag_service_for_tenant(tenant_id)
    top_k = k if k is not None else int(getattr(get_settings(), "rag_top_k", 5))
    run_id = audit.start_run(tenant_id=logical_tenant_id, user_id=user_id, text=text)
    hits = service.retrieve(
        text,
        run_id=run_id,
        user_id=user_id,
        agent=agent,
        roles=_parse_csv(roles),
        k=top_k,
    )
    audit.record(run_id, "run_finished", {"status": "completed", "result_count": len(hits)})
    rows = [
        {
            "chunk_id": hit.chunk.id,
            "document_id": hit.chunk.document_id,
            "title": hit.chunk.title,
            "uri": hit.chunk.uri,
            "score": hit.score,
            "source": hit.source,
            "metadata": hit.chunk.metadata,
            "text": hit.chunk.text,
        }
        for hit in hits
    ]
    if as_json:
        print(
            json.dumps(
                {"tenant_id": logical_tenant_id, "hits": rows},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"[ok] {len(rows)} hits for tenant {logical_tenant_id}")
        for index, row in enumerate(rows, start=1):
            snippet = " ".join(str(row["text"]).split())[:240]
            print(f"{index}. {row['score']:.3f} {row['title']} {row['uri']}")
            print(f"   {snippet}")
    return 0


def _rag_eval(
    dataset: str,
    *,
    tenant_id: str | None,
    min_hit_rate: float,
    min_mrr: float,
    as_json: bool,
) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    configure_logging()
    from agentkit.config import get_settings
    from agentkit.core.rag.eval import RAGEvalCase, evaluate_retriever
    from agentkit.core.rag.loaders import load_eval_dataset

    logical_tenant_id, service = _rag_service_for_tenant(tenant_id)
    default_k = int(getattr(get_settings(), "rag_top_k", 5))
    cases = [
        RAGEvalCase.from_dict(raw, default_tenant_id=logical_tenant_id, default_k=default_k)
        for raw in load_eval_dataset(dataset)
    ]
    report = evaluate_retriever(
        cases,
        retriever=service.retriever,
        default_tenant_id=logical_tenant_id,
    )
    if as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"RAG eval: cases={report.case_count}, hit_rate={report.hit_rate:.2%}, "
            f"recall={report.mean_recall:.2%}, precision={report.mean_precision:.2%}, "
            f"mrr={report.mrr:.3f}"
        )
    return 0 if report.gate(min_hit_rate=min_hit_rate, min_mrr=min_mrr) else 1


def build_parser() -> argparse.ArgumentParser:
    """构建公开 CLI 解析器，便于帮助文本和单元测试共用。"""
    parser = argparse.ArgumentParser(prog="agentkit")
    parser.add_argument(
        "--tenant",
        default=None,
        help="Tenant id (filename of tenants/<id>.json). Defaults to "
        "$AGENTKIT_TENANT_ID or company_alpha.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run-demo", help="Run the HR ranking demo task.")
    sub.add_parser("web", help="Start the Flask management console.")
    browser_login = sub.add_parser(
        "browser-login",
        help="Open a persistent browser profile for an interactive site login.",
    )
    browser_login.add_argument("site", choices=["xhs"], help="Site adapter to authenticate.")
    browser_login.add_argument(
        "--query",
        default="AI Agent",
        help="Search query used to verify the authenticated result page.",
    )
    browser_login.add_argument(
        "--target",
        choices=["search", "publish"],
        default="search",
        help="Open the search page or Creator Center publish page.",
    )
    sub.add_parser(
        "init-db",
        help="Create/verify storage (data dir + Postgres pgvector schema) and check connectivity.",
    )
    doctor = sub.add_parser(
        "doctor",
        help="Run deployment preflight checks (storage, catalog, tenant runtime).",
    )
    doctor.add_argument("--skip-db", action="store_true", help="Skip storage connectivity checks.")
    doctor.add_argument("--json", action="store_true", help="Emit JSON report.")

    ocr_check = sub.add_parser(
        "ocr-check",
        help="Run one real image through the configured shared OCR provider.",
    )
    ocr_check.add_argument(
        "image",
        help="PNG, JPEG or WebP image used for OCR verification.",
    )
    ocr_check.add_argument("--json", action="store_true", help="Emit JSON result.")

    new_tenant = sub.add_parser("new-tenant", help="Scaffold a new tenant config.")
    new_tenant.add_argument("tenant_id", help="Tenant id (becomes tenants/<id>.json).")
    new_tenant.add_argument("--force", action="store_true", help="Overwrite if it exists.")

    new_agent = sub.add_parser("new-agent", help="Scaffold a declarative Agent Manifest.")
    new_agent.add_argument("agent_id", help="Agent id, e.g. finance_assistant.")

    new_skill = sub.add_parser("new-skill", help="Scaffold a declarative Skill package.")
    new_skill.add_argument("package_id", help="Skill package id, e.g. invoice-query.")

    validate_catalog = sub.add_parser(
        "validate-catalog",
        help="Validate declarative Agent, Skill and Tool manifests.",
    )
    validate_catalog.add_argument("--json", action="store_true", help="Emit JSON report.")

    validate_contexts = sub.add_parser(
        "validate-contexts",
        help="Validate Context Packs, tenant overrides, hashes and token budgets.",
    )
    validate_contexts.add_argument("--json", action="store_true", help="Emit JSON report.")

    ev = sub.add_parser("eval", help="Run a golden dataset and enforce a regression gate.")
    ev.add_argument("dataset", help="Path to a .jsonl or .json golden dataset.")
    ev.add_argument(
        "--target",
        choices=["llm", "gateway", "gateway-trace"],
        default="llm",
        help=(
            "Evaluate raw LLM prompts (llm), rendered gateway text (gateway), "
            "or full response/audit JSON (gateway-trace)."
        ),
    )
    ev.add_argument(
        "--threshold",
        type=float,
        default=1.0,
        help="Minimum pass rate for the gate (0..1). Default 1.0.",
    )
    ev.add_argument(
        "--min-mean-score",
        type=float,
        default=0.0,
        help="Minimum mean weighted score for the gate (0..1). Default 0.0.",
    )
    ev.add_argument("--no-judge", action="store_true", help="Skip LLM-as-judge checks.")
    ev.add_argument("--json", action="store_true", help="Emit the full report as JSON.")

    eval_suite = sub.add_parser(
        "eval-suite",
        help="Run or validate a versioned enterprise evaluation suite.",
    )
    eval_suite.add_argument("suite", help="Path to an evaluation suite YAML file.")
    eval_suite.add_argument("--no-judge", action="store_true", help="Disable LLM-as-judge.")
    eval_suite.add_argument("--output", help="Persist the versioned JSON report to this path.")
    eval_suite.add_argument("--baseline", help="Compare the run with a previous JSON report.")
    eval_suite.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate suite configuration and executable checks without running targets.",
    )
    eval_suite.add_argument("--json", action="store_true", help="Emit JSON output.")

    rag_ingest = sub.add_parser(
        "rag-ingest",
        help="Ingest a file or folder into the configured RAG knowledge store.",
    )
    rag_ingest.add_argument("path", help="File or folder containing pdf/docx/txt/md/html/json/csv.")
    rag_ingest.add_argument(
        "--roles",
        default="",
        help="Comma-separated business roles allowed to retrieve these chunks. Empty = all roles.",
    )
    rag_ingest.add_argument(
        "--ocr",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable/disable OCR for scanned PDFs and embedded Word images.",
    )
    rag_ingest.add_argument("--json", action="store_true", help="Emit JSON report.")

    rag_query = sub.add_parser("rag-query", help="Query the configured RAG knowledge store.")
    rag_query.add_argument("text", help="Search query.")
    rag_query.add_argument("--agent", default="", help="Agent name for diagnostics/filtering.")
    rag_query.add_argument("--user-id", default="", help="User id for diagnostics.")
    rag_query.add_argument("--roles", default="", help="Comma-separated trusted business roles.")
    rag_query.add_argument("--k", type=int, default=None, help="Top-k hits.")
    rag_query.add_argument("--json", action="store_true", help="Emit JSON hits.")

    rag_eval = sub.add_parser("rag-eval", help="Run deterministic retrieval eval for RAG.")
    rag_eval.add_argument("dataset", help="JSON/JSONL cases with query and relevant ids.")
    rag_eval.add_argument("--min-hit-rate", type=float, default=0.0)
    rag_eval.add_argument("--min-mrr", type=float, default=0.0)
    rag_eval.add_argument("--json", action="store_true", help="Emit JSON report.")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "run-demo":
        _run_demo(tenant_id=args.tenant)
    elif args.command == "web":
        if args.tenant:
            os.environ["AGENTKIT_TENANT_ID"] = args.tenant
        _run_web()
    elif args.command == "browser-login":
        raise SystemExit(
            _browser_login(
                args.site,
                query=args.query,
                target=args.target,
                tenant_id=args.tenant,
            )
        )
    elif args.command == "init-db":
        raise SystemExit(_init_db())
    elif args.command == "doctor":
        raise SystemExit(_doctor(tenant_id=args.tenant, skip_db=args.skip_db, as_json=args.json))
    elif args.command == "ocr-check":
        raise SystemExit(_ocr_check(args.image, as_json=args.json))
    elif args.command == "new-tenant":
        _new_tenant(args.tenant_id, force=args.force)
    elif args.command == "new-agent":
        _new_agent(args.agent_id)
    elif args.command == "new-skill":
        _new_skill(args.package_id)
    elif args.command == "validate-catalog":
        raise SystemExit(_validate_catalog(as_json=args.json))
    elif args.command == "validate-contexts":
        raise SystemExit(_validate_contexts(tenant_id=args.tenant, as_json=args.json))
    elif args.command == "eval":
        code = _run_eval(
            args.dataset,
            target_kind=args.target,
            threshold=args.threshold,
            min_mean_score=args.min_mean_score,
            use_judge=not args.no_judge,
            as_json=args.json,
            tenant_id=args.tenant,
        )
        raise SystemExit(code)
    elif args.command == "eval-suite":
        raise SystemExit(
            _run_eval_suite(
                args.suite,
                use_judge=not args.no_judge,
                as_json=args.json,
                tenant_id=args.tenant,
                output_path=args.output,
                baseline_path=args.baseline,
                validate_only=args.validate_only,
            )
        )
    elif args.command == "rag-ingest":
        raise SystemExit(
            _rag_ingest(
                args.path,
                tenant_id=args.tenant,
                roles=args.roles,
                ocr=args.ocr,
                as_json=args.json,
            )
        )
    elif args.command == "rag-query":
        raise SystemExit(
            _rag_query(
                args.text,
                tenant_id=args.tenant,
                agent=args.agent,
                user_id=args.user_id,
                roles=args.roles,
                k=args.k,
                as_json=args.json,
            )
        )
    elif args.command == "rag-eval":
        raise SystemExit(
            _rag_eval(
                args.dataset,
                tenant_id=args.tenant,
                min_hit_rate=args.min_hit_rate,
                min_mrr=args.min_mrr,
                as_json=args.json,
            )
        )


if __name__ == "__main__":
    main()
