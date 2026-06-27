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
    """Verify PG connectivity and ensure the pgvector extension + schema exist."""
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
        PgVectorStore(settings)._ensure_schema()
        print("[ok] memories table + index ready")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] could not ensure memories schema: {exc}", file=sys.stderr)
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

    backend = settings.vector_store_backend
    print(f"[..] vector_store_backend = {backend}")
    if backend == "postgres":
        ok = _check_postgres(settings) and ok
    else:
        print("[ok] sqlite vector store (no external database required)")

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

    from agentkit.eval import LLMJudge, llm_target, load_cases, make_gateway_target, run_eval

    cases = load_cases(dataset)
    if target_kind == "gateway":
        runtime = build_runtime(tenant_id=tenant_id)
        target = make_gateway_target(runtime)
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

    new_tenant = sub.add_parser("new-tenant", help="Scaffold a new tenant config.")
    new_tenant.add_argument("tenant_id", help="Tenant id (becomes tenants/<id>.json).")
    new_tenant.add_argument("--force", action="store_true", help="Overwrite if it exists.")

    new_pack = sub.add_parser("new-pack", help="Scaffold a new domain pack.")
    new_pack.add_argument("domain", help="Domain string, e.g. billing.invoices.")
    new_pack.add_argument("--force", action="store_true", help="Overwrite if it exists.")

    ev = sub.add_parser("eval", help="Run a golden dataset and enforce a regression gate.")
    ev.add_argument("dataset", help="Path to a .jsonl or .json golden dataset.")
    ev.add_argument(
        "--target",
        choices=["llm", "gateway"],
        default="llm",
        help="Evaluate raw LLM prompts (llm) or the full agent gateway (gateway).",
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
    elif args.command == "new-tenant":
        _new_tenant(args.tenant_id, force=args.force)
    elif args.command == "new-pack":
        _new_pack(args.domain, force=args.force)
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
