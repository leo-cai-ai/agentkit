"""Filesystem-backed skill packages.

The store supports a Codex/Cursor-style layout:

    skills/
      skill-name/
        SKILL.md
        scripts/
        references/
        assets/

Runtime skill names may use dots, e.g. `candidate.rank`. Those names map to
folder names by replacing dots and underscores with hyphens.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .registry import SkillRegistry

RESOURCE_DIRS = {
    "scripts": ("scripts",),
    "references": ("references", "reference"),
    "assets": ("assets",),
}


@dataclass(frozen=True)
class SkillPackage:
    folder_name: str
    name: str
    description: str
    body: str
    root_path: Path
    skill_file: Path
    frontmatter: dict[str, str]
    resources: dict[str, list[str]]


class SkillFileStore:
    def __init__(self, root_path: str | Path, *, display_root: str | Path | None = None) -> None:
        self.root_path = Path(root_path).resolve()
        self.display_root = Path(display_root).resolve() if display_root else self.root_path.parent

    def list_packages(self) -> list[SkillPackage]:
        if not self.root_path.exists():
            return []
        packages = []
        for child in sorted(self.root_path.iterdir(), key=lambda item: item.name):
            if child.is_dir():
                package = self.load(child.name)
                if package:
                    packages.append(package)
        return packages

    def load(self, name: str) -> SkillPackage | None:
        folder = self.root_path / normalize_skill_folder(name)
        if not folder.exists() or not folder.is_dir():
            return None

        skill_file = find_skill_file(folder)
        if skill_file is None:
            return None

        frontmatter, body = parse_skill_markdown(skill_file.read_text(encoding="utf-8"))
        package_name = frontmatter.get("name") or folder.name
        return SkillPackage(
            folder_name=folder.name,
            name=package_name,
            description=frontmatter.get("description", ""),
            body=body.strip(),
            root_path=folder,
            skill_file=skill_file,
            frontmatter=frontmatter,
            resources=list_resources(folder),
        )

    def load_for_runtime_skill(self, runtime_name: str) -> SkillPackage | None:
        package = self.load(runtime_name)
        if package:
            return package
        return self.load(normalize_skill_folder(runtime_name))

    def create(
        self,
        *,
        name: str,
        description: str,
        body: str = "",
        resource_dirs: list[str] | None = None,
        overwrite: bool = False,
    ) -> SkillPackage:
        folder_name = normalize_skill_folder(name)
        folder = self.root_path / folder_name
        if folder.exists() and not overwrite:
            raise FileExistsError(f"Skill folder already exists: {folder}")
        folder.mkdir(parents=True, exist_ok=True)

        skill_file = folder / "SKILL.md"
        if skill_file.exists() and not overwrite:
            raise FileExistsError(f"Skill file already exists: {skill_file}")

        markdown = format_skill_markdown(
            name=folder_name,
            description=description,
            body=body or "Describe when and how to use this skill.",
        )
        skill_file.write_text(markdown, encoding="utf-8")

        for resource_dir in resource_dirs or []:
            if resource_dir not in RESOURCE_DIRS:
                raise ValueError(f"Unsupported resource directory: {resource_dir}")
            (folder / RESOURCE_DIRS[resource_dir][0]).mkdir(exist_ok=True)

        package = self.load(folder_name)
        assert package is not None
        return package

    def update(
        self,
        *,
        name: str,
        description: str | None = None,
        body: str | None = None,
        resource_dirs: list[str] | None = None,
    ) -> SkillPackage:
        package = self.load(name)
        if package is None:
            raise FileNotFoundError(f"Skill not found: {name}")

        markdown = format_skill_markdown(
            name=package.name,
            description=description if description is not None else package.description,
            body=body if body is not None else package.body,
        )
        package.skill_file.write_text(markdown, encoding="utf-8")

        for resource_dir in resource_dirs or []:
            if resource_dir not in RESOURCE_DIRS:
                raise ValueError(f"Unsupported resource directory: {resource_dir}")
            (package.root_path / RESOURCE_DIRS[resource_dir][0]).mkdir(exist_ok=True)

        updated = self.load(name)
        assert updated is not None
        return updated

    def read_resource(self, *, name: str, relative_path: str) -> str:
        package = self.load(name)
        if package is None:
            raise FileNotFoundError(f"Skill not found: {name}")
        path = safe_resource_path(package.root_path, relative_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Resource not found: {relative_path}")
        return path.read_text(encoding="utf-8")

    def write_resource(self, *, name: str, relative_path: str, content: str) -> Path:
        package = self.load(name)
        if package is None:
            raise FileNotFoundError(f"Skill not found: {name}")
        path = safe_resource_path(package.root_path, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def display_path(self, path: str | Path) -> str:
        resolved = Path(path).resolve()
        try:
            display = resolved.relative_to(self.display_root)
        except ValueError:
            display = resolved
        return str(display).replace("\\", "/")


def attach_skill_packages(*, skills: SkillRegistry, store: SkillFileStore) -> None:
    for skill in skills.all():
        package = store.load_for_runtime_skill(skill.name)
        if package is None:
            continue
        skills.register(
            replace(
                skill,
                skill_folder=store.display_path(package.root_path),
                skill_file=store.display_path(package.skill_file),
                skill_instructions=package.body,
                skill_resources=package.resources,
            )
        )


def normalize_skill_folder(name: str) -> str:
    normalized = name.strip().lower().replace("_", "-").replace(".", "-")
    chars = []
    previous_dash = False
    for char in normalized:
        if char.isalnum():
            chars.append(char)
            previous_dash = False
        elif not previous_dash:
            chars.append("-")
            previous_dash = True
    return "".join(chars).strip("-")


def find_skill_file(folder: Path) -> Path | None:
    for filename in ("SKILL.md", "skill.md"):
        path = folder / filename
        if path.exists() and path.is_file():
            return path
    return None


def parse_skill_markdown(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter delimiter '---'")

    frontmatter_lines: list[str] = []
    body_start = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body_start = index + 1
            break
        frontmatter_lines.append(line)

    if body_start is None:
        raise ValueError("SKILL.md frontmatter is missing closing delimiter '---'")

    frontmatter = parse_simple_yaml(frontmatter_lines)
    if not frontmatter.get("name") or not frontmatter.get("description"):
        raise ValueError("SKILL.md frontmatter requires name and description")
    return frontmatter, "\n".join(lines[body_start:])


def parse_simple_yaml(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"Unsupported frontmatter line: {raw_line}")
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def list_resources(folder: Path) -> dict[str, list[str]]:
    resources: dict[str, list[str]] = {name: [] for name in RESOURCE_DIRS}
    for resource_name, aliases in RESOURCE_DIRS.items():
        for alias in aliases:
            path = folder / alias
            if path.exists() and path.is_dir():
                resources[resource_name].extend(
                    str(item.relative_to(folder)).replace("\\", "/")
                    for item in sorted(path.rglob("*"), key=lambda entry: str(entry))
                    if item.is_file() and is_user_resource(item)
                )
    return resources


def is_user_resource(path: Path) -> bool:
    ignored_parts = {"__pycache__", ".pytest_cache", ".mypy_cache"}
    if any(part in ignored_parts for part in path.parts):
        return False
    return path.suffix not in {".pyc", ".pyo"}


def safe_resource_path(skill_root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError("Resource path must be relative to the skill folder")
    resolved_root = skill_root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Resource path cannot leave the skill folder") from exc
    return resolved


def format_skill_markdown(*, name: str, description: str, body: str) -> str:
    return (
        f"---\nname: {normalize_skill_folder(name)}\ndescription: {description}\n"
        f"---\n\n{body.strip()}\n"
    )
