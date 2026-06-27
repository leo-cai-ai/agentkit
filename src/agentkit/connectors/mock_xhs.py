"""Mock Xiaohongshu connector for the social-growth demo."""

from __future__ import annotations

from datetime import datetime, timedelta


class MockXhsConnector:
    def __init__(self) -> None:
        self._notes = [
            {
                "note_id": "XHS-001",
                "title": "30-day AI workflow rebuild diary",
                "author": "OpsLab",
                "likes": 42800,
                "saves": 11200,
                "comments": 928,
                "hook": "Show the before/after result in the first sentence.",
                "structure": "pain point -> concrete workflow -> measurable result -> checklist",
                "tags": ["AI workflow", "productivity", "case study"],
                "insight": (
                    "High-performing posts make the outcome tangible "
                    "before explaining the method."
                ),
            },
            {
                "note_id": "XHS-002",
                "title": "How I used agents to cut weekly reporting time",
                "author": "GrowthPM",
                "likes": 31900,
                "saves": 9700,
                "comments": 642,
                "hook": "Open with a relatable bottleneck and a hard number.",
                "structure": "scenario -> agent setup -> prompt template -> result screenshot",
                "tags": ["agent", "work automation", "template"],
                "insight": (
                    "Templates and screenshots increase saves " "because readers can reuse them."
                ),
            },
            {
                "note_id": "XHS-003",
                "title": "Enterprise AI assistant launch checklist",
                "author": "AIBuilder",
                "likes": 27100,
                "saves": 8600,
                "comments": 531,
                "hook": "Promise a checklist that avoids common launch mistakes.",
                "structure": "mistakes -> checklist -> operating cadence -> metrics",
                "tags": ["enterprise AI", "checklist", "operations"],
                "insight": (
                    "Checklist-style posts convert strategic topics " "into actionable reading."
                ),
            },
            {
                "note_id": "XHS-004",
                "title": "My first viral B2B AI note",
                "author": "ContentOps",
                "likes": 22600,
                "saves": 7100,
                "comments": 388,
                "hook": "Share the failed attempts before the winning version.",
                "structure": "failed drafts -> winning angle -> metrics -> reusable formula",
                "tags": ["B2B marketing", "AI content", "growth"],
                "insight": (
                    "Comparison between failures and winners "
                    "makes abstract writing advice credible."
                ),
            },
            {
                "note_id": "XHS-005",
                "title": "From zero to 10k followers with one niche series",
                "author": "NicheStudio",
                "likes": 19800,
                "saves": 6900,
                "comments": 412,
                "hook": "State the 30-day target and the daily posting system.",
                "structure": "goal -> niche definition -> daily series -> review loop",
                "tags": ["follower growth", "content system", "series"],
                "insight": "A named repeatable series helps audiences understand why to follow.",
            },
        ]

    def get_top_notes(self, *, topic: str, limit: int) -> list[dict]:
        ranked = sorted(
            self._notes,
            key=lambda item: item["likes"] + item["saves"] * 2 + item["comments"] * 3,
            reverse=True,
        )
        return [dict(item, topic=topic) for item in ranked[:limit]]

    def create_publish_package(self, *, article: dict, mode: str) -> dict:
        scheduled_for = datetime.now().astimezone() + timedelta(hours=2)
        return {
            "channel": "xiaohongshu",
            "mode": mode,
            "status": "draft_created" if mode == "draft" else "scheduled_for_review",
            "scheduled_for": scheduled_for.strftime("%Y-%m-%d %H:%M:%S %z"),
            "title": article.get("title", ""),
            "requires_real_connector": True,
        }
