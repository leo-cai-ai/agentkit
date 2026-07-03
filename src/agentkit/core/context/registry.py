"""Context Pack 的严格加载、租户覆盖与内容指纹。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import ContextDefinition, ContextDefinitionModel
from .sources import ContextSourceRegistry

MANDATORY_FRAGMENTS = ("security-boundary", "untrusted-data", "no-hidden-reasoning")
_TEMPLATE_VARIABLE = re.compile(r"{{\s*([a-z][a-z0-9_]*)\s*}}")
_ALLOWED_OVERRIDE_FILES = frozenset({"system.md", "user.md"})


class ContextRegistry:
    """在 Runtime 启动时一次性加载并冻结所有 Context Pack。"""

    def __init__(
        self,
        *,
        root: Path,
        tenant_selector: str,
        overrides: dict[str, str] | None = None,
        sources: ContextSourceRegistry | None = None,
        global_token_limit: int = 128_000,
    ) -> None:
        self._root = root.resolve()
        self._tenant_selector = tenant_selector
        self._sources = sources or ContextSourceRegistry.default()
        self._global_token_limit = int(global_token_limit)
        self._items = self._load_all(overrides or {})

    @property
    def root(self) -> Path:
        return self._root

    def get(self, context_id: str) -> ContextDefinition:
        try:
            return self._items[context_id]
        except KeyError as exc:
            raise KeyError(f"未注册 Context ID: {context_id}") from exc

    def manifest(self) -> list[dict[str, object]]:
        return [
            {
                "id": item.model.id,
                "version": item.model.version,
                "hash": item.content_hash,
                "override_hash": item.override_hash,
                "max_input_tokens": item.model.limits.max_input_tokens,
                "response_reserve_tokens": item.model.limits.response_reserve_tokens,
            }
            for item in sorted(self._items.values(), key=lambda value: value.model.id)
        ]

    @property
    def manifest_hash(self) -> str:
        payload = json.dumps(
            self.manifest(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _sha256(payload)

    def _load_all(self, overrides: dict[str, str]) -> dict[str, ContextDefinition]:
        items: dict[str, ContextDefinition] = {}
        context_files = sorted((self._root / "runtime").glob("**/context.yaml"))
        context_files.extend(sorted((self._root / "skills").glob("**/context.yaml")))
        for context_file in context_files:
            item = self._load_one(context_file)
            context_id = item.model.id
            if context_id in items:
                raise ValueError(f"重复的 Context ID: {context_id}")
            items[context_id] = item

        unknown = sorted(set(overrides) - set(items))
        if unknown:
            raise ValueError(f"Override 引用了未知 Context ID: {', '.join(unknown)}")
        for context_id, relative_path in sorted(overrides.items()):
            items[context_id] = self._apply_override(items[context_id], relative_path)
        return items

    def _load_one(self, context_file: Path) -> ContextDefinition:
        raw = _load_yaml_mapping(context_file)
        try:
            model = ContextDefinitionModel.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"{context_file}: Context 定义无效: {exc}") from exc
        self._validate_identity(context_file, model)
        self._validate_contract(context_file, model)

        source_dir = context_file.parent.resolve()
        system_path = _resolve_within(
            source_dir, model.templates.system, label="System 模板"
        )
        user_path = _resolve_within(source_dir, model.templates.user, label="User 模板")
        system_template = _read_required_text(system_path)
        user_template = _read_required_text(user_path)
        self._validate_system_template(system_template, system_path)
        self._validate_template_variables(model, user_template, user_path)

        fragment_names = tuple(dict.fromkeys((*MANDATORY_FRAGMENTS, *model.fragments)))
        fragment_contents: list[str] = []
        for name in fragment_names:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                raise ValueError(f"{context_file}: Fragment 名称无效: {name}")
            path = _resolve_within(
                self._root / "fragments",
                f"{name}.md",
                label="Fragment",
            )
            fragment_contents.append(_read_required_text(path))

        output_schema: dict[str, Any] | None = None
        if model.output.schema_path:
            schema_path = _resolve_within(
                source_dir,
                model.output.schema_path,
                label="Output Schema",
            )
            output_schema = _load_json_mapping(schema_path)

        content_hash = _definition_hash(
            model=model,
            system_template=system_template,
            user_template=user_template,
            fragment_names=fragment_names,
            fragment_contents=tuple(fragment_contents),
            output_schema=output_schema,
        )
        return ContextDefinition(
            model=model,
            source_dir=source_dir,
            system_template=system_template,
            user_template=user_template,
            fragments=tuple(fragment_contents),
            output_schema=output_schema,
            content_hash=content_hash,
        )

    def _validate_identity(self, context_file: Path, model: ContextDefinitionModel) -> None:
        try:
            if context_file.is_relative_to(self._root / "runtime"):
                relative = context_file.parent.relative_to(self._root / "runtime")
                expected_owner = "runtime"
            else:
                relative = context_file.parent.relative_to(self._root / "skills")
                expected_owner = "skill"
        except ValueError as exc:
            raise ValueError(f"{context_file}: Context 文件不在受管目录") from exc
        expected_id = f"{expected_owner}." + ".".join(relative.parts)
        if model.owner != expected_owner or model.id != expected_id:
            raise ValueError(
                f"{context_file}: 目录要求 id={expected_id}, owner={expected_owner}，"
                f"实际为 id={model.id}, owner={model.owner}"
            )

    def _validate_contract(self, context_file: Path, model: ContextDefinitionModel) -> None:
        if (
            model.limits.max_input_tokens + model.limits.response_reserve_tokens
            > self._global_token_limit
        ):
            raise ValueError(
                f"{context_file}: Context Token 预算超过全局上限 {self._global_token_limit}"
            )
        names: set[str] = set()
        for item in model.inputs:
            if item.name in names:
                raise ValueError(f"{context_file}: 重复 Input 名称: {item.name}")
            names.add(item.name)
            self._sources.require_source(item.source)
            self._sources.require_serializer(item.serializer)
            self._sources.require_truncator(item.truncate)
            if any(
                item.source == excluded or item.source.startswith(f"{excluded}.")
                for excluded in model.exclude
            ):
                raise ValueError(f"{context_file}: Input Source 同时被 exclude 禁止: {item.source}")

    def _validate_template_variables(
        self,
        model: ContextDefinitionModel,
        template: str,
        path: Path,
    ) -> None:
        declared = {item.name for item in model.inputs}
        referenced = set(_TEMPLATE_VARIABLE.findall(template))
        unknown = sorted(referenced - declared)
        if unknown:
            raise ValueError(f"{path}: 未声明模板变量: {', '.join(unknown)}")

    def _validate_system_template(self, template: str, path: Path) -> None:
        referenced = sorted(set(_TEMPLATE_VARIABLE.findall(template)))
        if referenced:
            raise ValueError(
                f"{path}: System 模板不能引用动态变量: {', '.join(referenced)}"
            )

    def _apply_override(
        self,
        definition: ContextDefinition,
        relative_path: str,
    ) -> ContextDefinition:
        raw_path = Path(relative_path)
        if raw_path.is_absolute():
            raise ValueError("Override 路径必须是工作区相对路径")
        if raw_path.parts and raw_path.parts[0] == self._root.name:
            candidate = (self._root.parent / raw_path).resolve()
        else:
            candidate = (self._root / raw_path).resolve()
        allowed_root = (self._root / "overrides" / self._tenant_selector).resolve()
        if not candidate.is_relative_to(allowed_root):
            raise ValueError(
                f"Override 路径必须位于 {allowed_root}: {relative_path}"
            )
        if not candidate.is_dir():
            raise ValueError(f"Override 目录不存在: {relative_path}")
        files = sorted(path for path in candidate.rglob("*") if path.is_file())
        invalid = [
            path
            for path in files
            if path.parent != candidate or path.name not in _ALLOWED_OVERRIDE_FILES
        ]
        if invalid:
            raise ValueError("Override 只允许 system.md 或 user.md")
        if not files:
            raise ValueError("Override 目录必须至少包含 system.md 或 user.md")

        system_template = definition.system_template
        user_template = definition.user_template
        system_path = candidate / "system.md"
        user_path = candidate / "user.md"
        if system_path.is_file():
            system_template = _read_required_text(system_path)
            self._validate_system_template(system_template, system_path)
        if user_path.is_file():
            user_template = _read_required_text(user_path)
            self._validate_template_variables(definition.model, user_template, user_path)
        override_hash = _sha256(
            _canonical_bytes(
                {
                    path.name: _normalize_text(path.read_text(encoding="utf-8"))
                    for path in files
                }
            )
        )
        return ContextDefinition(
            model=definition.model,
            source_dir=definition.source_dir,
            system_template=system_template,
            user_template=user_template,
            fragments=definition.fragments,
            output_schema=definition.output_schema,
            content_hash=definition.content_hash,
            override_hash=override_hash,
        )


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"文件不存在: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: YAML 无效: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: YAML 根节点必须是对象")
    return value


def _load_json_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: JSON Schema 无效: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON Schema 根节点必须是对象")
    return value


def _resolve_within(base: Path, relative: str, *, label: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute():
        raise ValueError(f"{label}路径越界: {relative}")
    resolved_base = base.resolve()
    resolved = (resolved_base / raw).resolve()
    if not resolved.is_relative_to(resolved_base):
        raise ValueError(f"{label}路径越界: {relative}")
    return resolved


def _read_required_text(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"文件不存在: {path}")
    text = _normalize_text(path.read_text(encoding="utf-8")).strip()
    if not text:
        raise ValueError(f"文件内容不能为空: {path}")
    return text


def _definition_hash(
    *,
    model: ContextDefinitionModel,
    system_template: str,
    user_template: str,
    fragment_names: tuple[str, ...],
    fragment_contents: tuple[str, ...],
    output_schema: dict[str, Any] | None,
) -> str:
    payload = {
        "definition": model.model_dump(mode="json", by_alias=True),
        "system": system_template,
        "user": user_template,
        "fragments": [
            {"name": name, "content": content}
            for name, content in zip(fragment_names, fragment_contents, strict=True)
        ],
        "output_schema": output_schema,
    }
    return _sha256(_canonical_bytes(payload))


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


__all__ = ["ContextRegistry", "MANDATORY_FRAGMENTS"]
