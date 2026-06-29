"""Console entry point for agentkit."""

from __future__ import annotations

import argparse
import json
import os
import sys
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


def _check_postgres(settings: Any) -> bool:
    """Verify PG connectivity and ensure configured Postgres schemas exist."""
    from agentkit.core.audit import PostgresAuditLog
    from agentkit.core.memory.pg_store import PgConversationStore
    from agentkit.core.memory.pg_vector_store import PgVectorStore
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
    from agentkit.runtime.bootstrap import DATA_DIR

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
    if storage_backend == "postgres" or vector_backend == "postgres":
        ok = _check_postgres(settings) and ok
    else:
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
    print(f"Created tenant config: {path}")
    print(f"Run it with: agentkit --tenant {tenant_id} run-demo")


def _new_pack(domain: str, *, force: bool) -> None:
    from pathlib import Path

    from agentkit.runtime.scaffold import create_pack

    src_root = Path(__file__).resolve().parent / "domain_packs"
    pack_dir = create_pack(domain, src_root=src_root, force=force)
    print(f"Created domain pack: {pack_dir}")
    print(f'Enable it by adding "{domain}" to a tenant\'s enabled_domains.')


def _validate_packs(*, domains: list[str], as_json: bool) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    configure_logging()

    from agentkit.runtime.pack_registry import validate_pack_contracts

    results = validate_pack_contracts(domains=set(domains) if domains else None)
    if as_json:
        print(json.dumps([result.to_dict() for result in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            status = "PASS" if result.passed else "FAIL"
            summary = (
                f"{len(result.agents)} agents, "
                f"{len(result.skills)} skills, "
                f"{len(result.tools)} tools"
            )
            print(
                f"[{status}] {result.domain}: "
                f"{summary}"
            )
            for error in result.errors:
                print(f"  error: {error}")
            for warning in result.warnings:
                print(f"  warning: {warning}")
    return 0 if all(result.passed for result in results) else 1


def _runtime_doctor_checks(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Return deployment preflight checks that do not call the LLM."""
    from agentkit.runtime.bootstrap import load_tenant_config, resolve_tenant_id
    from agentkit.runtime.pack_registry import discover_packs, validate_pack_contracts

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

    enabled_domains = [
        str(domain) for domain in tenant_config.get("enabled_domains", []) if str(domain)
    ]
    if enabled_domains:
        add("enabled domains", True, ", ".join(enabled_domains))
    else:
        add("enabled domains", False, "tenant has no enabled_domains")

    discovered = discover_packs()
    missing_domains = sorted(set(enabled_domains) - set(discovered))
    add(
        "domain discovery",
        not missing_domains,
        "missing: " + ", ".join(missing_domains)
        if missing_domains
        else "all enabled domains found",
    )

    pack_results = validate_pack_contracts(domains=set(enabled_domains))
    for result in pack_results:
        detail = (
            f"{len(result.agents)} agents, {len(result.skills)} skills, "
            f"{len(result.tools)} tools"
        )
        if result.errors:
            detail += "; errors: " + "; ".join(result.errors)
        if result.warnings:
            detail += "; warnings: " + "; ".join(result.warnings)
        add(f"pack contract: {result.domain}", result.passed, detail)

    try:
        runtime = build_runtime(tenant_id=resolved_tenant)
    except Exception as exc:  # noqa: BLE001
        add("runtime build", False, str(exc))
        return checks
    add("runtime build", True, f"tenant_id={runtime.tenant_config.get('tenant_id')}")

    registered_agents = {agent.name for agent in runtime.gateway.agents.all()}
    registered_skills = {skill.name for skill in runtime.gateway.skills.all()}
    registered_tools = {tool.name for tool in runtime.gateway.tools.all()}

    chat_agents = tenant_config.get("chat_agents", [])
    if not isinstance(chat_agents, list):
        add("tenant chat_agents", False, "chat_agents must be a list")
    else:
        for index, item in enumerate(chat_agents):
            if not isinstance(item, dict):
                add(f"tenant chat_agents[{index}]", False, "entry must be an object")
                continue
            name = str(item.get("name") or "")
            mode = str(item.get("mode") or "chat")
            if not name:
                add(f"tenant chat_agents[{index}]", False, "name is required")
                continue
            if name not in registered_agents:
                add(f"tenant chat_agents[{name}]", False, "agent is not registered")
                continue
            if mode != "chat":
                add(
                    f"tenant chat_agents[{name}]",
                    False,
                    "mode must be 'chat' when provided",
                )
                continue
            actions_raw = item.get("actions_enabled", None)
            if not isinstance(actions_raw, bool):
                add(
                    f"tenant chat_agents[{name}]",
                    False,
                    "actions_enabled must be an explicit boolean",
                )
                continue
            kind = "action" if actions_raw else "answer"
            add(f"tenant chat_agents[{name}]", True, f"mode={mode}, kind={kind}")

    approval_required = tenant_config.get("approval_required_skills", [])
    if not isinstance(approval_required, list):
        add("approval_required_skills", False, "must be a list")
    else:
        missing = sorted(
            str(skill) for skill in approval_required if skill not in registered_skills
        )
        add(
            "approval_required_skills",
            not missing,
            "missing: " + ", ".join(missing) if missing else "all approval skills registered",
        )

    routing_hints = tenant_config.get("routing_hints", {})
    if not isinstance(routing_hints, dict):
        add("routing_hints", False, "must be an object")
    else:
        missing = sorted(str(skill) for skill in routing_hints if skill not in registered_skills)
        add(
            "routing_hints",
            not missing,
            "missing: " + ", ".join(missing) if missing else "all hinted skills registered",
        )

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


def main() -> None:
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
    sub.add_parser(
        "init-db",
        help="Create/verify storage (data dir + Postgres pgvector schema) and check connectivity.",
    )
    doctor = sub.add_parser(
        "doctor",
        help="Run deployment preflight checks (storage, packs, tenant runtime).",
    )
    doctor.add_argument("--skip-db", action="store_true", help="Skip storage connectivity checks.")
    doctor.add_argument("--json", action="store_true", help="Emit JSON report.")

    new_tenant = sub.add_parser("new-tenant", help="Scaffold a new tenant config.")
    new_tenant.add_argument("tenant_id", help="Tenant id (becomes tenants/<id>.json).")
    new_tenant.add_argument("--force", action="store_true", help="Overwrite if it exists.")

    new_pack = sub.add_parser("new-pack", help="Scaffold a new domain pack.")
    new_pack.add_argument("domain", help="Domain string, e.g. billing.invoices.")
    new_pack.add_argument("--force", action="store_true", help="Overwrite if it exists.")

    validate_packs = sub.add_parser(
        "validate-packs",
        help="Validate domain-pack registration contracts for plugin/CI smoke checks.",
    )
    validate_packs.add_argument(
        "domains",
        nargs="*",
        help="Optional domain(s) to validate. Defaults to every discovered pack.",
    )
    validate_packs.add_argument("--json", action="store_true", help="Emit JSON report.")

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

    args = parser.parse_args()
    if args.command == "run-demo":
        _run_demo(tenant_id=args.tenant)
    elif args.command == "web":
        if args.tenant:
            os.environ["AGENTKIT_TENANT_ID"] = args.tenant
        _run_web()
    elif args.command == "init-db":
        raise SystemExit(_init_db())
    elif args.command == "doctor":
        raise SystemExit(_doctor(tenant_id=args.tenant, skip_db=args.skip_db, as_json=args.json))
    elif args.command == "new-tenant":
        _new_tenant(args.tenant_id, force=args.force)
    elif args.command == "new-pack":
        _new_pack(args.domain, force=args.force)
    elif args.command == "validate-packs":
        raise SystemExit(_validate_packs(domains=args.domains, as_json=args.json))
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


if __name__ == "__main__":
    main()
