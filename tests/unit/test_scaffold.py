"""Unit tests for new-tenant / new-pack scaffolding."""

from __future__ import annotations

import importlib
import json
import sys

import pytest

from agentkit.runtime import scaffold


def test_render_tenant_config_is_valid_json() -> None:
    text = scaffold.render_tenant_config("acme")
    data = json.loads(text)
    assert data["tenant_id"] == "acme"
    assert "enabled_domains" in data


def test_create_tenant_writes_file(tmp_path) -> None:
    path = scaffold.create_tenant("acme", root=tmp_path)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tenant_id"] == "acme"


def test_create_tenant_refuses_overwrite(tmp_path) -> None:
    scaffold.create_tenant("acme", root=tmp_path)
    with pytest.raises(FileExistsError):
        scaffold.create_tenant("acme", root=tmp_path)


def test_create_tenant_force_overwrites(tmp_path) -> None:
    scaffold.create_tenant("acme", root=tmp_path)
    path = scaffold.create_tenant("acme", root=tmp_path, force=True)
    assert path.is_file()


def test_create_pack_is_discoverable(tmp_path, monkeypatch) -> None:
    """A generated pack written into a temp package must be importable and expose
    DOMAIN + register."""
    pkg_root = tmp_path / "extpacks"
    (pkg_root).mkdir()
    (pkg_root / "__init__.py").write_text("", encoding="utf-8")

    pack_dir = scaffold.create_pack("billing.invoices", src_root=pkg_root)
    assert pack_dir.is_dir()
    assert (pack_dir / "pack.py").is_file()
    assert (pack_dir / "__init__.py").is_file()

    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("extpacks.billing_invoices.pack")
    try:
        assert module.DOMAIN == "billing.invoices"
        assert callable(module.register)
    finally:
        for name in list(sys.modules):
            if name.startswith("extpacks"):
                del sys.modules[name]


def test_create_pack_refuses_overwrite(tmp_path) -> None:
    pkg_root = tmp_path / "extpacks"
    pkg_root.mkdir()
    scaffold.create_pack("billing.invoices", src_root=pkg_root)
    with pytest.raises(FileExistsError):
        scaffold.create_pack("billing.invoices", src_root=pkg_root)
