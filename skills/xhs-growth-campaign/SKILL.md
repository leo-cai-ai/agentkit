---
name: xhs.growth.campaign
description: Run a governed Xiaohongshu growth workflow with isolated research, analysis, copy, publishing, and metrics steps.
---

# Xiaohongshu Growth Campaign

Use this skill when a user asks for a Xiaohongshu or RedNote growth workflow,
including researching top posts/videos, comparing cases, drafting content,
preparing a publishing package, or tracking a follower-growth KPI.

## Workflow

1. `xhs.trend.research`: collect today's top notes/videos for the topic and requested `top_n`.
2. `xhs.case.extract`: extract hooks, structure, engagement, and case signals.
3. `xhs.case.compare`: compare reusable patterns across hooks, structures, saves, and comments.
4. `xhs.strategy.plan`: build the 30-day KPI plan for the follower-growth target.
5. `xhs.copy.generate`: generate grounded title, outline, body, tags, and CTA.
6. `xhs.copy.review`: check brand safety, groundedness, and risky claims.
7. `xhs.publish.prepare`: prepare a draft publishing package through the configured RPA provider.
8. `xhs.metrics.track`: initialize KPI tracking for the campaign.

Each step writes a compact summary plus an artifact reference. Downstream steps
should read only the summaries or specific artifacts they need; do not carry
full raw case/video payloads through every prompt.

## Governance

Publishing-related runs require human approval before execution. The default
connector creates a draft package and does not post to an external account.
生产 RPA provider 必须保持超时、审计、Artifact 和审批语义。Provider 和工具适配均位于
本 Skill 的 `scripts/providers.py` 与 `scripts/tools.py`；运行时通过 `skill.yaml`
中的受控工具工厂按租户创建它们。
