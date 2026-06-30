"""Stable content contract shared by Xiaohongshu publishing providers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

PUBLISH_MEDIA_STRATEGIES = frozenset({"upload", "xhs_text_image"})


def normalize_publish_content(article: dict[str, Any]) -> dict[str, Any]:
    """Return the exact user-visible content covered by review and approval."""

    title = _clean(article.get("title"))
    body = str(article.get("body") or "").strip()
    tags = _tags(article.get("tags"), body=body)
    media_paths = [
        str(item).strip() for item in article.get("media_paths", []) if str(item).strip()
    ]
    media_strategy = _clean(article.get("media_strategy")).lower() or "upload"
    card_text = str(article.get("card_text") or "").strip()
    card_style = _clean(article.get("card_style"))
    return {
        "title": title,
        "body": body,
        "tags": tags,
        "media_paths": media_paths,
        "media_strategy": media_strategy,
        "card_text": card_text,
        "card_style": card_style,
    }


def publication_content_hash(content: dict[str, Any]) -> str:
    normalized = normalize_publish_content(content)
    media = []
    for raw_path in normalized.pop("media_paths"):
        path = Path(raw_path).expanduser()
        media.append(
            {
                "path": str(path),
                "sha256": _file_hash(path) if path.is_file() else "missing",
            }
        )
    normalized["media"] = media
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_publish_content(
    article: dict[str, Any],
    *,
    default_media_strategy: str,
    default_card_style: str,
) -> dict[str, Any]:
    """Apply the configured media policy before freezing a publish package."""

    content = normalize_publish_content(article)
    requested_strategy = _clean(article.get("media_strategy")).lower()
    default_strategy = validate_publish_media_strategy(default_media_strategy)
    if requested_strategy:
        strategy = validate_publish_media_strategy(requested_strategy)
    elif content["media_paths"]:
        strategy = "upload"
    else:
        strategy = default_strategy

    content["media_strategy"] = strategy
    if strategy == "xhs_text_image":
        card_style = _clean(article.get("card_style") or default_card_style)
        if not card_style:
            raise ValueError("XHS text-image style must not be empty")
        content["media_paths"] = []
        content["card_text"] = str(article.get("card_text") or content["body"]).strip()
        content["card_style"] = card_style
    else:
        content["card_text"] = ""
        content["card_style"] = ""
    return content


def validate_publish_media_strategy(value: Any) -> str:
    strategy = _clean(value).lower()
    if strategy not in PUBLISH_MEDIA_STRATEGIES:
        supported = ", ".join(sorted(PUBLISH_MEDIA_STRATEGIES))
        raise ValueError(
            f"Unsupported XHS media strategy: {strategy!r}. " f"Supported strategies: {supported}."
        )
    return strategy


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def append_hashtags(body: str, tags: list[str]) -> str:
    existing = {tag.lower() for tag in re.findall(r"#([^#\s]+)", body)}
    missing = [tag for tag in tags if tag.lower() not in existing]
    if not missing:
        return body.strip()
    suffix = " ".join(f"#{tag}" for tag in missing)
    return f"{body.strip()}\n\n{suffix}".strip()


def _tags(value: Any, *, body: str) -> list[str]:
    raw = list(value) if isinstance(value, list | tuple) else []
    raw.extend(re.findall(r"#([^#\s]+)", body))
    seen: set[str] = set()
    tags: list[str] = []
    for item in raw:
        tag = _clean(item).lstrip("#")
        key = tag.lower()
        if tag and key not in seen:
            seen.add(key)
            tags.append(tag)
    return tags[:10]


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


__all__ = [
    "append_hashtags",
    "normalize_publish_content",
    "publication_content_hash",
    "resolve_publish_content",
    "validate_publish_media_strategy",
]
