"""Discover business domain packs without hardcoding imports.

A domain pack is a module exposing two attributes:

* ``DOMAIN``: the domain string a tenant turns on via ``enabled_domains``.
* ``register(*, agents, skills, tools, tenant_config)``: registration entrypoint.

Packs are found two ways:

1. In-repo scan of ``agentkit.domain_packs.*`` (each subpackage's ``pack`` module).
2. Installed plugins declaring the ``agentkit.domain_packs`` entry point group.

Entry-point packs are loaded last and may override in-repo packs of the same
domain. A pack that fails to import is logged and skipped, never fatal.
"""

from __future__ import annotations

import logging
from importlib import import_module
from importlib.metadata import EntryPoint, entry_points
from pkgutil import iter_modules
from typing import Any, Protocol

from agentkit import domain_packs

logger = logging.getLogger("agentkit.packs")

ENTRY_POINT_GROUP = "agentkit.domain_packs"


class RegisterFn(Protocol):
    def __call__(
        self,
        *,
        agents: Any,
        skills: Any,
        tools: Any,
        tenant_config: dict,
    ) -> None: ...


def iter_entry_points(*, group: str) -> list[EntryPoint]:
    """Wrapper around importlib.metadata for easy monkeypatching in tests."""
    return list(entry_points(group=group))


def _pack_from_module(module: object) -> tuple[str, RegisterFn] | None:
    domain = getattr(module, "DOMAIN", None)
    register = getattr(module, "register", None)
    if not isinstance(domain, str) or not callable(register):
        return None
    return domain, register  # type: ignore[return-value]


def discover_packs() -> dict[str, RegisterFn]:
    """Return ``domain -> register`` for every discoverable pack, sorted by domain."""
    found: dict[str, RegisterFn] = {}

    # 1. In-repo scan.
    for module_info in iter_modules(domain_packs.__path__, domain_packs.__name__ + "."):
        if not module_info.ispkg:
            continue
        pack_module_name = f"{module_info.name}.pack"
        try:
            module = import_module(pack_module_name)
        except Exception:  # pragma: no cover - exercised via monkeypatch
            logger.warning("Skipping pack %s: import failed", pack_module_name, exc_info=True)
            continue
        pack = _pack_from_module(module)
        if pack is None:
            logger.warning("Skipping %s: missing DOMAIN or register", pack_module_name)
            continue
        found[pack[0]] = pack[1]

    # 2. Entry-point plugins (may override in-repo packs).
    for entry_point in iter_entry_points(group=ENTRY_POINT_GROUP):
        try:
            module = entry_point.load()
        except Exception:
            logger.warning("Skipping entry point %s: load failed", entry_point.name, exc_info=True)
            continue
        pack = _pack_from_module(module)
        if pack is None:
            logger.warning("Skipping entry point %s: missing DOMAIN or register", entry_point.name)
            continue
        found[pack[0]] = pack[1]

    return {domain: found[domain] for domain in sorted(found)}


__all__ = ["discover_packs", "RegisterFn", "ENTRY_POINT_GROUP"]
