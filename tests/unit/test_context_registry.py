from __future__ import annotations

from pathlib import Path

import pytest

from agentkit.core.context.registry import ContextRegistry
from tests.context_support import write_context_pack


def test_registry_loads_pack_and_builds_stable_manifest(tmp_path: Path) -> None:
    write_context_pack(tmp_path)

    first = ContextRegistry(root=tmp_path, tenant_selector="company_alpha")
    second = ContextRegistry(root=tmp_path, tenant_selector="company_alpha")

    item = first.get("runtime.intent")
    assert item.content_hash.startswith("sha256:")
    assert first.manifest() == second.manifest()
    assert first.manifest_hash == second.manifest_hash
    assert first.manifest()[0]["id"] == "runtime.intent"


def test_registry_hash_changes_when_template_changes(tmp_path: Path) -> None:
    folder = write_context_pack(tmp_path)
    first = ContextRegistry(root=tmp_path, tenant_selector="company_alpha")
    (folder / "system.md").write_text("CHANGED", encoding="utf-8")

    second = ContextRegistry(root=tmp_path, tenant_selector="company_alpha")

    assert first.get("runtime.intent").content_hash != second.get("runtime.intent").content_hash


def test_registry_rejects_undeclared_template_variable(tmp_path: Path) -> None:
    folder = write_context_pack(tmp_path)
    (folder / "user.md").write_text("{{ secret }}", encoding="utf-8")

    with pytest.raises(ValueError, match="未声明模板变量"):
        ContextRegistry(root=tmp_path, tenant_selector="company_alpha")


def test_registry_rejects_malformed_template_syntax(tmp_path: Path) -> None:
    folder = write_context_pack(tmp_path)
    (folder / "user.md").write_text("{{ invalid-name }}", encoding="utf-8")

    with pytest.raises(ValueError, match="模板语法无效"):
        ContextRegistry(root=tmp_path, tenant_selector="company_alpha")


def test_registry_rejects_dynamic_variables_in_system_template(tmp_path: Path) -> None:
    folder = write_context_pack(tmp_path)
    (folder / "system.md").write_text("SYSTEM {{ message }}", encoding="utf-8")

    with pytest.raises(ValueError, match="System 模板不能引用动态变量"):
        ContextRegistry(root=tmp_path, tenant_selector="company_alpha")


def test_registry_rejects_unknown_source(tmp_path: Path) -> None:
    write_context_pack(
        tmp_path,
        inputs=[{"name": "raw", "source": "request.raw_context"}],
    )

    with pytest.raises(ValueError, match="未注册 Context Source"):
        ContextRegistry(root=tmp_path, tenant_selector="company_alpha")


def test_registry_rejects_pack_above_global_token_limit(tmp_path: Path) -> None:
    write_context_pack(tmp_path, max_input_tokens=2000, response_reserve_tokens=300)

    with pytest.raises(ValueError, match="Token 预算"):
        ContextRegistry(
            root=tmp_path,
            tenant_selector="company_alpha",
            global_token_limit=100,
        )


def test_registry_rejects_invalid_output_schema_at_startup(tmp_path: Path) -> None:
    folder = write_context_pack(tmp_path)
    (folder / "output.schema.json").write_text(
        '{"type":"not-a-json-schema-type"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="JSON Schema 定义无效"):
        ContextRegistry(root=tmp_path, tenant_selector="company_alpha")


def test_registry_rejects_template_path_escape(tmp_path: Path) -> None:
    folder = write_context_pack(tmp_path)
    context_file = folder / "context.yaml"
    context_file.write_text(
        context_file.read_text(encoding="utf-8").replace("system.md", "../../outside.md"),
        encoding="utf-8",
    )
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError, match="路径越界"):
        ContextRegistry(root=tmp_path, tenant_selector="company_alpha")


def test_registry_applies_only_selected_tenant_text_override(tmp_path: Path) -> None:
    write_context_pack(tmp_path)
    override = tmp_path / "overrides" / "company_alpha" / "runtime.intent"
    override.mkdir(parents=True)
    (override / "system.md").write_text("TENANT SYSTEM", encoding="utf-8")

    registry = ContextRegistry(
        root=tmp_path,
        tenant_selector="company_alpha",
        overrides={"runtime.intent": "overrides/company_alpha/runtime.intent"},
    )

    item = registry.get("runtime.intent")
    assert item.system_template == "TENANT SYSTEM"
    assert item.override_hash.startswith("sha256:")


def test_override_must_stay_under_selected_tenant(tmp_path: Path) -> None:
    write_context_pack(tmp_path)

    with pytest.raises(ValueError, match="Override 路径"):
        ContextRegistry(
            root=tmp_path,
            tenant_selector="company_alpha",
            overrides={"runtime.intent": "overrides/other/runtime.intent"},
        )


def test_override_rejects_policy_files(tmp_path: Path) -> None:
    write_context_pack(tmp_path)
    override = tmp_path / "overrides" / "company_alpha" / "runtime.intent"
    override.mkdir(parents=True)
    (override / "context.yaml").write_text("id: runtime.intent", encoding="utf-8")

    with pytest.raises(ValueError, match="只允许 system.md 或 user.md"):
        ContextRegistry(
            root=tmp_path,
            tenant_selector="company_alpha",
            overrides={"runtime.intent": "overrides/company_alpha/runtime.intent"},
        )
