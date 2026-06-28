"""Manage filesystem skill packages for AgentKit.

Examples:

    python tools/skill_tool.py list
    python tools/skill_tool.py show candidate-rank
    python tools/skill_tool.py add policy-qa \
        --description "Answer policy questions" --resources references scripts
    python tools/skill_tool.py update policy-qa --description "Answer HR policy questions"
    python tools/skill_tool.py read-resource candidate-rank references/scoring.md
    python tools/skill_tool.py write-resource policy-qa references/policy.md \
        --body-file policy.md
    python tools/skill_tool.py validate
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentkit.core.skill_store import SkillFileStore

AGENTKIT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read and write AgentKit skill folders.")
    parser.add_argument(
        "--root",
        default=str(AGENTKIT_ROOT / "skills"),
        help="Skill root directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List available filesystem skills.")

    show_parser = subparsers.add_parser("show", help="Show one skill package.")
    show_parser.add_argument("name")

    add_parser = subparsers.add_parser("add", help="Add a new skill folder.")
    add_parser.add_argument("name")
    add_parser.add_argument("--description", required=True)
    add_parser.add_argument("--body", default="")
    add_parser.add_argument("--body-file")
    add_parser.add_argument(
        "--resources",
        nargs="*",
        default=["references"],
        choices=["scripts", "references", "assets"],
    )
    add_parser.add_argument("--overwrite", action="store_true")

    create_parser = subparsers.add_parser("create", help="Alias for add.")
    create_parser.add_argument("name")
    create_parser.add_argument("--description", required=True)
    create_parser.add_argument("--body", default="")
    create_parser.add_argument("--body-file")
    create_parser.add_argument(
        "--resources",
        nargs="*",
        default=["references"],
        choices=["scripts", "references", "assets"],
    )
    create_parser.add_argument("--overwrite", action="store_true")

    update_parser = subparsers.add_parser("update", help="Update an existing skill SKILL.md.")
    update_parser.add_argument("name")
    update_parser.add_argument("--description")
    update_parser.add_argument("--body")
    update_parser.add_argument("--body-file")
    update_parser.add_argument(
        "--resources",
        nargs="*",
        choices=["scripts", "references", "assets"],
        help="Ensure these resource directories exist.",
    )

    read_resource_parser = subparsers.add_parser("read-resource", help="Read a resource file.")
    read_resource_parser.add_argument("name")
    read_resource_parser.add_argument("path")

    write_resource_parser = subparsers.add_parser(
        "write-resource", help="Create or replace a resource file."
    )
    write_resource_parser.add_argument("name")
    write_resource_parser.add_argument("path")
    write_resource_parser.add_argument("--body", default="")
    write_resource_parser.add_argument("--body-file")

    subparsers.add_parser("validate", help="Validate all skill folders.")

    args = parser.parse_args()
    store = SkillFileStore(args.root, display_root=AGENTKIT_ROOT)

    if args.command == "list":
        rows = [
            {
                "folder": package.folder_name,
                "name": package.name,
                "description": package.description,
                "skill_file": store.display_path(package.skill_file),
                "scripts": len(package.resources.get("scripts", [])),
                "references": len(package.resources.get("references", [])),
                "assets": len(package.resources.get("assets", [])),
            }
            for package in store.list_packages()
        ]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if args.command == "show":
        package = store.load(args.name)
        if package is None:
            print(f"Skill not found: {args.name}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "folder": package.folder_name,
                    "name": package.name,
                    "description": package.description,
                    "skill_file": store.display_path(package.skill_file),
                    "resources": package.resources,
                    "body": package.body,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command in {"add", "create"}:
        body = read_body_arg(args.body, args.body_file)
        package = store.create(
            name=args.name,
            description=args.description,
            body=body,
            resource_dirs=args.resources,
            overwrite=args.overwrite,
        )
        print(f"Created {store.display_path(package.skill_file)}")
        return 0

    if args.command == "update":
        body = read_body_arg(args.body, args.body_file) if args.body or args.body_file else None
        package = store.update(
            name=args.name,
            description=args.description,
            body=body,
            resource_dirs=args.resources,
        )
        print(f"Updated {store.display_path(package.skill_file)}")
        return 0

    if args.command == "read-resource":
        print(store.read_resource(name=args.name, relative_path=args.path))
        return 0

    if args.command == "write-resource":
        body = read_body_arg(args.body, args.body_file)
        path = store.write_resource(name=args.name, relative_path=args.path, content=body)
        print(f"Wrote {store.display_path(path)}")
        return 0

    if args.command == "validate":
        packages = store.list_packages()
        if not packages:
            print("No skill packages found.", file=sys.stderr)
            return 1
        for package in packages:
            print(f"OK {package.folder_name}: {package.description}")
        return 0

    return 1


def read_body_arg(body: str, body_file: str | None) -> str:
    if body_file:
        return Path(body_file).read_text(encoding="utf-8")
    return body


if __name__ == "__main__":
    raise SystemExit(main())
