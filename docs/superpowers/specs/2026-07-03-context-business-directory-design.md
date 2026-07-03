# Context Business 目录重命名设计

## 目标

消除仓库根目录 `skills/` 与 `contexts/skills/` 的命名歧义，同时保持业务能力包和 LLM 节点上下文契约的职责分离。

## 最终目录语义

```text
skills/<package>/
  SKILL.md                 # 跨平台业务说明唯一来源
  skill.yaml               # AgentKit Capability/Tool 执行契约
  scripts/                 # Handler、Workflow 与 Tool 实现

contexts/business/<package>/<node>/
  context.yaml             # 单次业务 LLM 节点的输入、预算与输出契约
  system.md
  user.md
  output.schema.json       # 仅 JSON 输出节点需要
```

`contexts/runtime/` 继续保存框架公共 LLM 节点；`contexts/business/` 只保存业务 Skill 内部的 LLM 节点。业务流程、脚本和完整业务规则不得复制到 Context Pack。

## Context ID 与归属

现有 Context ID 保持不变，避免调用方、审计事件、Golden 文件和租户 Override 无意义改名：

- `skill.candidate-rank.summary`
- `skill.xhs-growth-campaign.article-generate`
- `skill.xhs-growth-campaign.content-review`

Skill 所有的 Context Pack 必须新增 `owner_skill`：

- `skill.candidate-rank.summary` → `candidate.rank`
- 两个小红书 Pack → `xhs.growth.campaign`

严格校验规则：

1. `owner: runtime` 时禁止声明 `owner_skill`。
2. `owner: skill` 时必须声明非空 `owner_skill`。
3. Registry 只扫描 `contexts/runtime/` 和 `contexts/business/`。
4. 旧 `contexts/skills/` 不保留兼容扫描；目录残留应由结构测试直接失败。

## 内容去重边界

- `SKILL.md`：业务用途、完整流程、业务边界和跨平台操作说明。
- `system.md`：仅保留当前 LLM 节点特有的输出格式、证据限制和禁止行为。
- `context.yaml`：声明是否通过 `instructions.skill: true` 注入 `SKILL.md` 正文。
- 不把 Workflow、Tool 清单或完整 Skill 说明复制到 Context Pack。

现有三个业务 Pack 的 System 内容已经是节点级约束，本次只做归属字段与目录迁移，不改变其渲染正文。

## 兼容性与 Hash

这是新项目，直接迁移，不保留旧目录兼容逻辑。Context ID 不变，但 `context.yaml` 增加 `owner_skill` 会改变 Pack Hash 和 Context Manifest Hash；这是预期的可审计变更。等待审批的旧 Checkpoint 将因 Hash 不一致而拒绝恢复，需要重新发起任务。

租户 Override 以 Context ID 为键，不依赖基础 Pack 的物理目录，因此无需修改现有 Override 配置格式。

## 测试与验收

1. 先写失败测试，要求 Registry 能从 `contexts/business/` 加载 11 个 Pack。
2. 校验 Skill Pack 缺少 `owner_skill`、Runtime Pack 错误声明 `owner_skill`时启动失败。
3. 校验仓库中不存在 `contexts/skills/`。
4. 运行 11 个 Golden Render；System/User 内容必须保持不变。
5. 运行单元测试、集成测试、Ruff、Mypy 和 `validate-contexts`。
6. 更新 README、架构文档、学习指南和 `contexts/README.md` 的目录说明。
