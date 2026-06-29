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
Production RPA providers must preserve timeout, audit, artifact, and approval
semantics. Replace provider implementations in
`src/agentkit/domain_packs/social_growth/providers.py` and keep tool adapters in
`src/agentkit/domain_packs/social_growth/tools.py` stable.
